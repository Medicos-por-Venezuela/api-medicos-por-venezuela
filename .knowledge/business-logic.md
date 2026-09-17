# Lógica de negocio a replicar (origen: app Next.js + Supabase)

Hoy la lógica vive en el frontend Next.js conectado directo a Supabase. Este backend FastAPI debe
replicarla en `src/services/`. Resumen accionable (fuente: `medicos-por-venezuela`).

## 1. Cola / "Atender al siguiente" (`panel-medico.tsx`)
La cola del panel es **en tiempo real** (Supabase Realtime `postgres_changes` sobre
`consultations`; migración `*_enable_realtime_on_consultations_for_panel.sql`): las consultas
nuevas aparecen de inmediato, **sin gate de espera** (el gate de 20 min se eliminó) y sin polling.
El backend la sirve en una pasada con `GET /consultations/panel` (espera + mías + cerradas).

La cola son los casos `status='waiting'` sin médico, **de las colas del médico** (§2), por orden de
llegada del paciente (`queued_at`; un caso derivado conserva la suya).
- **Toma atómica** (dos rutas equivalentes): `POST /queue/{id}/take` con
  `with_for_update(nowait=True)` y `POST /consultations/{id}/claim` con
  `UPDATE ... WHERE status='waiting' AND assigned_doctor_id IS NULL` (`rowcount == 0` → 409). La
  base elige al único ganador; **prohibido** replicar la toma con read-then-write (ver
  security.md §Concurrencia).
- Campos al tomar: `status='in_progress'`, `assigned_doctor_id`, `opened_at` (no pisar si existe)
  y **`video_room_url` en el mismo UPDATE** (`coalesce` con una sala nueva). Desde 2026-09-17 la
  atención es **siempre por videoconsulta**: `via_whatsapp=true` → 422.

## 2. Cola por especialidad — `services/queue_access.py` (2026-09-17)
**La columna del match es `consultations.specialty_id`** contra `users.specialty_id`. Una sola
función (`queue_scope`) decide qué ve y qué puede tomar cada uno; la usan panel, claim, `/queue`,
`/queue/{id}/take` y la derivación desde la cola:
1. Admin/super_admin: todas las colas, **salvo** si su especialidad es `mental_health_only`
   (Psicología): entonces la regla normal.
2. Sin especialidad o con una `is_placeholder` ("Otra"): ninguna cola (`queue_blocked_reason` en el
   panel), hasta que actualice su perfil.
3. Resto: su especialidad exacta + las de `specialty_queue_access` (sembradas: Psiquiatría →
   Psicología, Medicina interna → Medicina general).

"Otra" no se puede pedir al crear una consulta (422). Un médico puede escribir una especialidad que
no está (`doctors.requested_specialty`); el admin la resuelve con
`POST /doctors/{id}/specialty-request/resolve`.

## 2b. Derivación a la cola de otra especialidad
- Desde la cola (caso sin tomar): `POST /consultations/{id}/derive` — el mismo caso cambia de
  `specialty_id`, guarda `derived_from_specialty_id` y conserva `queued_at`.
- Desde el detalle (caso atendido): `POST /consultations/{id}/refer-to-queue` — el padre queda
  `referred_to_specialist` (firmado, `closed_at`), y una hija `waiting` sin médico entra a la cola
  destino con el `queued_at` del padre. Motivo y autor en el evento `derived`.
- Destinos (`GET /consultations/derivation-targets`): activas, no de relleno y con al menos un
  médico habilitado en esa cola. Al paciente se le avisa por correo con el enlace a su sala.
- `POST /consultations/{id}/refer` (cita con fecha y médico) queda por compatibilidad; el panel ya
  no lo usa.

## 3. Transiciones de estado (`consultations.status`)
Válidos: `waiting | in_progress | referred_to_specialist | urgent_in_person | closed | cancelled | patient_no_show` (+ `closed_by_admin` en la base real).
- Tomar: `waiting → in_progress` (médico). Cerrar: `in_progress → closed` (+ `closed_at`, `internal_note`).
- No-show: `in_progress → patient_no_show`. Admin puede ir a cualquier estado, reasignar médico, editar nota.
- Regla admin: no poner `in_progress` sin `assigned_doctor_id`.
- Cada transición registra un `consultation_events` (`opened`/`closed`/`patient_no_show`/`admin_update`).

## 4. Presencia (dos mecanismos DISTINTOS)
- **Paciente (heartbeat a la BD, se mantiene):** la sala de espera llama
  `POST /consultations/{id}/heartbeat` (anon) → `patient_last_seen_at = now()` (solo si
  `waiting`/`in_progress`). "Presente" si < 5 min (cola) — la UI del panel usa su propia ventana.
- **Médico (Supabase Realtime Presence, SIN base de datos):** el estado "en línea" de los médicos
  ya **no** usa heartbeat ni columna: es **Presence app-level** (canal `online-doctors`,
  `track/untrack` + `presenceState`, ver `lib/presence.tsx` del frontend). El backend no participa;
  para filtrar el pool por online, el **cliente** le pasa los `user_ids` que Presence sabe online
  (`GET /doctors/pool?online=&online_ids=`) y el backend filtra IN/NOT IN.
- Vestigios a limpiar (pendiente): la RPC `mark_myself_online()` y la columna
  `profiles.last_seen_at` siguen en la BD pero **ya nadie las escribe** — no basar lógica nueva
  en ellas.

## 5. Roles / autorización
Ver `@.claude/rules/security.md`. `is_staff` / `is_admin`; revocación = `active=false`;
`set_my_role` solo `patient|doctor`.

## 6. Código de consulta
En la base real lo genera SIEMPRE el trigger `generate_consultation_code` (`CONS-YYYYMMDD-NNNN`).
La API **no** debe fijarlo (cualquier `code` enviado se ignora).

## 7. Videoconsulta (Jitsi) — idempotente
Sala `https://{JITSI_DOMAIN}/vamed-{uuid}` guardada en `consultations.video_room_url`. El default
es la instancia **self-hosted abierta** `meet.medicosporvenezuela.org` — NO el público
`meet.jit.si`, que hoy exige login de moderador ("no moderators have yet arrived"). La crea el
**claim** (ver §1). `POST /consultations/{id}/video-room` la devuelve si existe y la crea si el caso
sigue abierto (`waiting`/`in_progress`/`contacted_whatsapp`), con escritura condicional. El
frontend además **sana** al abrir las salas legacy guardadas con `meet.jit.si` (ver
`browserRoomUrl` en `lib/jitsi.ts`).

## 8. Sala de espera en vivo — `services/waiting_room.py`
`GET /consultations/{id}/waiting-room` (JSON) y `/waiting-room/stream` (SSE). Fases: `waiting`
(sin médico) · `ready` (médico y sala: trae `video_room_url` y `doctor_name`) · `scheduled` ·
`finished`. Sigue la cadena hacia abajo: si el paciente fue derivado responde por la hija, con un
`access_token` para ella. El stream abre una sesión corta por lectura (`get_session_factory`), late
cada `WAITING_ROOM_HEARTBEAT_SECONDS` y se corta a los `WAITING_ROOM_STREAM_MAX_SECONDS`.

## Estado de portado
- `POST /queue/{id}/take` ✅ y `POST /consultations/{id}/claim` ✅ (toma atómica; el panel usa claim).
- `GET /consultations/panel` ✅ (espera + mías + cerradas en una pasada; Realtime avisa, esto trae).
- `POST /queue/attend-next` ✅ (selección por `can_attend`/presencia/especialidad/FIFO + lock).
- `GET /specialties` ✅ (catálogo + reglas, fuente única con el frontend).
- `POST /consultations/{id}/close` ✅ (closed/no-show + evento, con anti-IDOR de pertenencia).
- `POST /consultations/{id}/heartbeat` ✅ (presencia del PACIENTE; la del médico es Presence, sin endpoint).
- `POST /consultations/{id}/video-room` ✅ (Jitsi idempotente, self-hosted).
- `GET /doctors/pool` ✅ (pool paginado con filtros; online lo aporta el cliente vía Presence).
- `POST /doctors/{id}/contact` ✅ (revela WhatsApp con registro en `audit_log`).
- `GET /auth/me` + `/auth/me/permissions` ✅ (perfil efectivo + gating de UI).
- `PATCH /profiles/{id}/active` ✅ (revocar/reactivar).
- `POST /profiles/{id}/finalize-role` ✅ (set_my_role).
- Derivación de prioridad/categoría al crear consulta ✅.

- **Autenticación (JWT de Supabase) + RBAC** ✅: `GET /auth/me`, gating staff/admin, scoping
  anti-IDOR del paciente, actor derivado del token. Médico revocado pierde acceso al instante.
- **Observabilidad** ✅ (Correlation-ID + logs JSON), **paginación** ✅ (máx. 100),
  **resiliencia** ✅ (`POST /queue/release-stale`).

### Pendiente (siguiente fase)
- Endpoint explícito de urgente presencial (hoy vía `PATCH /consultations/{id}`).
- `GET /consultations` y `GET /consultations/{id}` siguen abiertos a cualquier staff, sin acotar por
  especialidad (la cola sí lo está).
- Worker/CRON real que invoque `release-stale` periódicamente (hoy es un endpoint admin).
