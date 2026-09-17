# Spec: Cola por especialidad, derivación a la cola y sala de espera en vivo

> Intención confirmada con el usuario vía `interview-me` (2026-09-17).
> Fases posteriores: [`plan.md`](./plan.md) y [`todo.md`](./todo.md).
> Abarca DOS repos: `api-medicos-por-venezuela` (dominio, rama base `dev`) y
> `medicos-por-venezuela` (UI, rama base `dev_aws`). La copia de este archivo en el frontend es
> idéntica; la fuente de verdad es esta.

## Objective

Que a cada paciente lo vea **solo la especialidad que le toca**, que **siempre** se atienda por
videoconsulta con la sala garantizada, que **derivar** sea mandarlo a la cola de otra
especialidad (sin cita) y que el paciente **nunca entre a una sala vacía**. Todo con la marca de
la web pública.

### Por qué ahora (datos de producción, 2026-09-17)

- Luis Bolívar (psicólogo, `super_admin`) ve casos de Medicina general: `get_panel` y `claim`
  saltan el filtro si `principal.is_admin`.
- La cola no filtra por especialidad: `can_attend_consultation` solo separa salud mental de
  salud física, así que un traumatólogo ve Medicina general, Urología, etc.
- Hay casos tomados sin sala (8 `in_progress` y 27 `closed` sin `video_room_url`): la toma por
  WhatsApp no crea sala y el panel se traga los fallos de `ensureVideoRoom`.
- `/sala-espera` muestra "Entrar a la videoconsulta" desde el registro, aunque nadie haya tomado
  el caso: el paciente entra a una sala vacía.
- Los pacientes derivados crean cuentas nuevas porque su panel no les muestra la derivación.
- 528 médicos tienen especialidad "Otra" (523 sin login en 30 días) y 43 consultas la usan: no
  identifica a ningún especialista.

### Usuarios

| Actor              | Qué gana                                                                                       |
| ------------------ | ---------------------------------------------------------------------------------------------- |
| Médico / psicólogo | Una cola solo con lo suyo, un único botón para atender (siempre video) y derivar sin agendar.  |
| Especialista       | Recibe en su cola a los pacientes derivados, con quién y por qué los derivó.                   |
| Paciente           | Sabe si ya hay médico antes de entrar; si lo derivan, lo ve en su panel sin crear otra cuenta. |
| Admin              | Ve y resuelve las especialidades que los médicos escribieron a mano.                           |

## Reglas de negocio

### R1. Quién ve qué en la cola (y quién puede tomarlo)

Una sola función decide ambas cosas (listar y tomar), en el servicio:

1. **Admin** (`admin`/`super_admin` activo): ve **todas** las colas, **salvo** que su
   especialidad sea exclusiva de salud mental (`specialties.mental_health_only`, hoy
   Psicología). En ese caso aplica la regla normal (Luis → solo Psicología).
2. **Médico sin especialidad o con especialidad de relleno** (`specialties.is_placeholder`,
   hoy "Otra"): **no ve nada** y no puede tomar nada, hasta que actualice su perfil.
3. **Resto**: coincidencia **exacta** de `consultations.specialty_id` con `users.specialty_id`,
   más los accesos extra de la tabla `specialty_queue_access`:
   - Psiquiatría → también ve Psicología.
   - Medicina interna → también ve Medicina general.

La cola es `status = 'waiting' AND assigned_doctor_id IS NULL`, ordenada por `queued_at`
(hora de llegada del paciente) y luego `created_at`.

Aplica a `GET /consultations/panel`, `POST /consultations/{id}/claim`, `GET /queue` y
`POST /queue/{id}/take`.

### R2. Atender = tomar con sala garantizada

- El claim es atómico (`UPDATE … WHERE assigned_doctor_id IS NULL AND status = 'waiting'`) y en
  el **mismo UPDATE** fija `video_room_url = coalesce(video_room_url, <sala nueva>)`. No existe
  un caso tomado sin sala por este camino.
- Desaparece la atención por WhatsApp: `via_whatsapp = true` → 422. El cuerpo del claim pasa a
  ser opcional.
- Al tomar, sale el correo "tu médico te está esperando" (ya existe).
- `POST /{id}/video-room` también crea sala para un caso `in_progress` sin ella (los legacy),
  pedida por su médico, un admin, el token o el paciente dueño.

### R3. Derivar desde la cola (caso sin tomar)

- `POST /consultations/{id}/derive` con `{specialty_id}`.
- Solo quien **ve** el caso en su cola (R1) puede derivarlo.
- Destino válido: especialidad activa, no borrada, no de relleno, distinta de la actual y con
  **al menos un médico habilitado** (ficha con credencial válida y cuenta activa).
- Escritura condicional sobre el **mismo caso** (`WHERE status='waiting' AND assigned_doctor_id
IS NULL AND specialty_id = <la que vio>`): cambia `specialty_id`, guarda
  `derived_from_specialty_id` y conserva `queued_at` (no pierde el turno). 0 filas → 409.
- Evento `derived` (sin motivo) + `audit_log` `consultation.derived`.
- Correo al paciente: "tu solicitud pasó a la cola de X".

### R4. Derivar con especialista desde el detalle (caso tomado)

- `POST /consultations/{id}/refer-to-queue` con `{specialty_id, reason, signature}`. Sin fecha.
- Solo el médico asignado o un admin. El padre debe estar abierto (`in_progress` /
  `contacted_whatsapp`).
- **Padre**: escritura condicional → `referred_to_specialist`, `closed_at = now`, firma. Evento
  `referred_to_specialist` con el motivo. Sale de "Mis pacientes" del primer médico.
- **Hija**: nueva consulta encadenada (`parent_consultation_id`), mismo paciente, motivo,
  categoría y prioridad; `specialty_id` = destino, `derived_from_specialty_id` = la del padre,
  `status = 'waiting'`, sin médico, **`queued_at` del padre** (conserva su turno), sin sala (se
  crea al tomarla). Evento `derived` con el motivo, autor = el médico que deriva.
- El especialista, al abrir la hija, ve el bloque "Derivado por Dr. X desde Y: motivo"
  (`derivation` en el detalle, resuelto del último evento `derived`).
- Correo al paciente: "te derivamos a X, estás en cola", con enlace a su sala de espera.
- El endpoint viejo `POST /{id}/refer` (cita con fecha y médico) se conserva para no romper un
  frontend desplegado por detrás; el panel deja de usarlo.

### R5. Sala de espera en vivo (paciente)

- `GET /consultations/{id}/waiting-room`: estado de la sala para el paciente. Autoriza el token
  de esa consulta, la sesión del paciente dueño o staff.
- **Sigue la cadena hacia abajo**: si el caso fue derivado, responde por la hija más reciente
  (`consultation_id` actual) y, si la credencial fue el token del caso original, entrega un
  `access_token` nuevo para la hija.
- Fases: `waiting` (en cola, sin médico) · `ready` (médico asignado y sala creada: trae
  `video_room_url` y `doctor_name`) · `scheduled` (cita agendada legacy) · `finished`.
- `GET /consultations/{id}/waiting-room/stream`: lo mismo por **SSE** (`text/event-stream`).
  Emite al conectar y en cada cambio; comentario de latido cada 15 s; se cierra cuando el caso termina
  (`finished`) o tras `WAITING_ROOM_STREAM_MAX_SECONDS` (el cliente reconecta). Consulta la
  base con sesiones cortas (no retiene una conexión del pool durante el stream).
- El frontend usa el stream y, si falla, cae a pedir el JSON cada 15 s.

### R6. "Otra"

- `specialties.is_placeholder` (true para "Otra") sustituye cualquier literal de nombre.
- Sale del selector del registro de paciente (y por tanto de "Solicitar consulta" desde Mi
  caso, que usa el mismo formulario). `POST /consultations` rechaza una especialidad de relleno
  (422).
- Migración de datos: las consultas `waiting` sin asignar con "Otra" pasan a Medicina general.
- Médico con "Otra": en su perfil elige de la lista o escribe su especialidad
  (`doctors.requested_specialty`). Mientras tanto no ve casos (R1.2).
- Admin: `GET /doctors/specialty-requests` lista las pendientes; `POST
/doctors/{id}/specialty-request/resolve` con `{specialty_id}` asigna (la especialidad nueva
  se crea antes con el CRUD de catálogo existente) y limpia la solicitud. Auditado.

### R7. Panel médico (UI)

- Tarjeta de la cola: título = especialidad (en vez de "Paciente"), "Derivado desde X" si
  aplica, botón **"Atender paciente"** y botón **"Derivar a especialista"** (modal con la lista
  → confirmación "¿Seguro que quieres derivar este paciente a X?").
- KPIs: solo **"Sin atender"** (= pacientes esperando en tu cola) y **"Consultas cerradas por
  mí"**.
- Aviso bloqueante para médicos con "Otra" o sin especialidad, con enlace a su perfil.

### R8. Detalle de consulta (UI)

- "Ver Pool de médicos (pedir interconsulta)".
- "Agendar con especialista" → **"Derivar con especialista"**: especialidad → motivo → firma,
  sin fecha.
- "Unirse a videoconsulta" siempre visible en un caso abierto; si no hay sala, se crea al pulsar.

### R9. Marca

Panel médico, admin, sala de espera y Mi caso con la marca de la web pública: barra navy
(`#18202b`) con el logo blanco, azul `#0066fe` en acciones principales, enlaces y foco, fuente
Nunito Sans. Los colores de estado (verde activo, ámbar abierta, rojo urgente) conservan su
significado.

## Fuera de alcance

- Bloquear cuentas o consultas duplicadas del paciente.
- "Agendar seguimiento" y las citas ya agendadas (`scheduled`, 92 en prod).
- WhatsApp automático.
- Borrar "Otra" del catálogo (tiene médicos ligados) o cambiar el registro de médicos.
- Eliminar cuentas de Auth.
- Restringir `GET /consultations` y `GET /consultations/{id}` por especialidad (hoy cualquier
  staff los lee): riesgo anotado, no se toca aquí.

## Tech Stack

- **Backend** — Python 3.12, FastAPI async, SQLAlchemy 2.0 (asyncpg), Pydantic v2, PostgreSQL 17
  (Supabase), `uv`, Ruff, pytest + pytest-asyncio.
- **Correo** — Mailtrap vía `src/services/mail.py` (best-effort).
- **Frontend** — Next.js 14 (Pages Router), TypeScript, Playwright (E2E), ESLint + Prettier, pnpm.

## Commands

```
API       uv run pytest -q --no-cov              (0 failed)
          uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto   (≥95%)
          uv run ruff check . --fix && uv run ruff format .
          python artisan make:migration "<desc>" / python artisan migrate
Frontend  pnpm exec tsc --noEmit · pnpm lint · pnpm format:check
          NEXT_DIST_DIR=.next-e2e pnpm build · pnpm test:e2e
```

## Testing Strategy

- **API**: tests de integración por regla (R1–R6) en `tests/test_cola_especialidad.py`,
  `tests/test_derivacion.py`, `tests/test_sala_espera.py`, `tests/test_especialidad_solicitada.py`;
  se actualizan los que fijaban el comportamiento anterior (`test_admin_ve_y_toma_toda_la_cola`,
  `test_claim_via_whatsapp_marca_el_flag`, tests de especialidades). El generador SSE se prueba
  como unidad con un proveedor de estados falso (httpx `ASGITransport` no entrega un stream
  incremental).
- **Frontend**: E2E para cada gating nuevo — botón único y sala creada, derivar desde la cola,
  derivar desde el detalle, sala de espera sin botón hasta que un médico toma el caso, aviso de
  "Otra". Se actualizan `panel-race`, `panel-atender-video`, `sala-espera`,
  `mi-caso-videoconsulta` y `registro-paciente`.

## Boundaries

- **Siempre**: escritura condicional en toda transición disputada; `audit_log` en la misma
  transacción; migraciones idempotentes; `extra="forbid"`; nada de PII en logs.
- **Preguntar antes**: escrituras a producción (las de esta tarea ya aprobadas: Deymen →
  Medicina general y baja de 5 admins, hechas el 2026-09-17).
- **Nunca**: literales de nombres de especialidad en código (van por columnas/tablas sembradas
  en migración); exponer datos nuevos por PostgREST.

## Success Criteria

- Luis solo ve Psicología; un traumatólogo no ve Medicina general; un internista sí.
- Un médico con "Otra" ve el aviso y la cola vacía; tras elegir especialidad ve la suya.
- Ningún caso tomado desde el panel queda sin `video_room_url`.
- `/sala-espera` y `/mi-caso` no muestran "Entrar" hasta que un médico toma el caso, y lo
  muestran solos (sin recargar) cuando ocurre.
- Un paciente derivado ve en `/mi-caso` el caso nuevo en cola, con su especialidad.
- Suite API en verde (0 failed, cobertura ≥95%), lint/format limpios, E2E en verde.
