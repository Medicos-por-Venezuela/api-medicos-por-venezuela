# Lógica de negocio a replicar (origen: app Next.js + Supabase)

Hoy la lógica vive en el frontend Next.js conectado directo a Supabase. Este backend FastAPI debe
replicarla en `src/services/`. Resumen accionable (fuente: `medicos-por-venezuela`).

## 1. Cola / "Atender al siguiente" (`panel-medico.tsx`)
Selección del próximo caso, en orden:
1. Filtrar por `canAttend(specialty, category, needs_tags)` (ver §2).
2. Preferir **pacientes presentes**: `patient_last_seen_at` dentro de los últimos **5 min**.
3. Entre los elegibles, preferir match de especialidad (`matchesSpecialty`), si no, el más antiguo (FIFO).
- **Toma atómica:** UPDATE condicional `... WHERE id = ? AND status = 'waiting'`. En esta API se
  hace con `with_for_update(nowait=True)` (ya implementado en `services/queue.py`).
- Campos al tomar: `status='in_progress'`, `assigned_doctor_id`, `opened_at` (no pisar si existe).

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

## 4. Heartbeats de presencia
- Paciente: RPC `mark_patient_waiting(cid)` cada **15 s** → `patient_last_seen_at = now()` (solo si
  `waiting`/`in_progress`). Presente si < 5 min. → endpoint sugerido `POST /consultations/{id}/heartbeat` (anon).
- Médico: `mark_myself_online()` cada **60 s** → `profiles.last_seen_at`. En línea si < 3 min.

## 5. Roles / autorización
Ver `@.claude/rules/security.md`. `is_staff` / `is_admin`; revocación = `active=false`;
`set_my_role` solo `patient|doctor`.

## 6. Código de consulta
En la base real lo genera SIEMPRE el trigger `generate_consultation_code` (`CONS-YYYYMMDD-NNNN`).
La API **no** debe fijarlo (cualquier `code` enviado se ignora).

## 7. Videoconsulta (Jitsi) — idempotente
Sala `https://{JITSI_DOMAIN|meet.jit.si}/vamed-{uuid}` guardada en `consultations.video_room_url`.
Generar **solo si** `video_room_url IS NULL` y `status='waiting'`; si ya existe, devolver la misma.
→ endpoint sugerido `POST /consultations/{id}/video-room`.

## Estado de portado
- `POST /queue/{id}/take` ✅ (lock atómico).
- `POST /queue/attend-next` ✅ (selección por `can_attend`/presencia/especialidad/FIFO + lock).
- `GET /specialties` ✅ (catálogo + reglas, fuente única con el frontend).
- `POST /consultations/{id}/close` ✅ (closed/no-show + evento).
- `POST /consultations/{id}/heartbeat` ✅ (presencia paciente).
- `POST /profiles/{id}/online` ✅ (presencia médico).
- `POST /consultations/{id}/video-room` ✅ (Jitsi idempotente).
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
