# Lógica de negocio a replicar (origen: app Next.js + Supabase)

Hoy la lógica vive en el frontend Next.js conectado directo a Supabase. Este backend FastAPI debe
replicarla en `src/services/`. Resumen accionable (fuente: `medicos-por-venezuela`).

## 1. Cola / "Atender al siguiente" (`panel-medico.tsx`)
La cola del panel es **en tiempo real** (Supabase Realtime `postgres_changes` sobre
`consultations`; migración `*_enable_realtime_on_consultations_for_panel.sql`): las consultas
nuevas aparecen de inmediato, **sin gate de espera** (el gate de 20 min se eliminó) y sin polling.
El backend la sirve en una pasada con `GET /consultations/panel` (espera + mías + cerradas).

Selección del próximo caso ("Atender al siguiente"), en orden:
1. Filtrar por `canAttend(specialty, category, needs_tags)` (ver §2).
2. Preferir **pacientes presentes**: `patient_last_seen_at` dentro de los últimos **5 min**
   (`PRESENCE_WINDOW` en `services/queue.py`).
3. Entre los elegibles, preferir match de especialidad (`matchesSpecialty`), si no, el más antiguo (FIFO).
- **Toma atómica** (dos rutas equivalentes): `POST /queue/{id}/take` con
  `with_for_update(nowait=True)` y `POST /consultations/{id}/claim` con
  `UPDATE ... WHERE assigned_doctor_id IS NULL` (`rowcount == 0` → 409). La base elige al único
  ganador; **prohibido** replicar la toma con read-then-write (ver security.md §Concurrencia).
- Campos al tomar: `status='in_progress'`, `assigned_doctor_id`, `opened_at` (no pisar si existe),
  `attended_via_whatsapp` (claim).

## 2. Matching de especialidades (`lib/utils.ts`) — PORTAR EXACTO
`SPECIALTY_NEEDS`: `Medicina general` y `Otra` cubren `['*']` (todo). El resto cubre necesidades
específicas. `RESERVED_NEEDS = { 'Apoyo emocional': ['Psicología','Psiquiatría'], 'Crisis de ansiedad': ['Psicología','Psiquiatría'] }`.

- `matchesSpecialty(spec, category, needs)`: true si `SPECIALTY_NEEDS[spec]` incluye `'*'` o alguno
  de `[category, ...needs]`.
- `canAttend(spec, category, needs)` (separación dura, dos direcciones):
  1. Toda necesidad reservada debe permitir esa especialidad (un general **nunca** ve casos de
     `Apoyo emocional`/`Crisis de ansiedad`).
  2. `Psicología` **solo** atiende casos con alguna necesidad reservada (no casos físicos).

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
`meet.jit.si`, que hoy exige login de moderador ("no moderators have yet arrived"). Generar
**solo si** `video_room_url IS NULL` y `status='waiting'`; si ya existe, devolver la misma
(`POST /consultations/{id}/video-room`). El frontend además **sana** al abrir las salas legacy
guardadas con `meet.jit.si` (ver `browserRoomUrl` en `lib/jitsi.ts`).

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
- Endpoints explícitos de derivación a especialista / urgente presencial (hoy vía `PATCH /consultations/{id}`).
- Worker/CRON real que invoque `release-stale` periódicamente (hoy es un endpoint admin).
