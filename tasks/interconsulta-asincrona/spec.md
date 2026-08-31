# Spec: Interconsulta asíncrona (paciente de consultorio)

> Intención confirmada con el usuario vía `interview-me` (2026-08-31).
> Fases posteriores: `tasks/interconsulta-asincrona/plan.md` y `todo.md`.
> Abarca DOS repos: `api-medicos-por-venezuela` (dominio) y `medicos-por-venezuela` (UI).

## Objective

Un médico documenta el caso de un paciente **de su consultorio** (que no está en la
plataforma ni pasa por la cola pública) y solicita una segunda opinión a un especialista.
La solicitud se difunde por **especialidad**; el primer especialista que la **toma** gana, y
a partir de ahí contacta al médico tratante **fuera** de la plataforma (WhatsApp/correo).

### Usuarios

| Actor | Qué gana |
|---|---|
| **Médico tratante** (general o de consultorio) | Acceso a especialistas para casos que hoy no puede traer a la plataforma. |
| **Especialista del pool** | Casos concretos de su especialidad, con el contexto clínico ya cargado. |

### Por qué ahora

Hoy la interconsulta (`POST /interconsultations`) solo existe **en vivo**, atada a una
consulta activa de la cola pública con video compartido. Los pacientes propios del médico —
la mayor parte de su práctica — quedan fuera. Este flujo abre ese canal sin exigir
simultaneidad de los dos médicos.

### Historias de usuario

1. Como médico tratante, registro a mi paciente de consultorio (nombre, edad, antecedentes)
   para poder describir su caso.
2. Como médico tratante, solicito una interconsulta eligiendo la **especialidad** que
   necesito, y todos los médicos de esa especialidad reciben un correo.
3. Como médico tratante, alternativamente elijo a **un médico específico**; solo él recibe
   el correo y yo veo su teléfono.
4. Como especialista, veo en mi bandeja las solicitudes abiertas de mi especialidad **sin
   identidad del paciente ni del médico tratante**, y decido si tomo el caso.
5. Como especialista, al **tomar** el caso recibo el contacto del médico tratante y lo
   contacto por WhatsApp/correo.
6. Como médico tratante, recibo un correo cuando un especialista toma mi caso.

## Tech Stack

- **Backend** — Python 3.12, FastAPI async, SQLAlchemy 2.0 (asyncpg), Pydantic v2,
  PostgreSQL 17 (Supabase), `uv`, Ruff, pytest + pytest-asyncio.
- **Correo** — Mailtrap vía `src/services/mail.py` (best-effort, nunca rompe el flujo).
- **Frontend** — Next.js (Pages Router), TypeScript, Playwright (E2E), ESLint + Prettier, pnpm.

## Commands

### API (`api-medicos-por-venezuela`)

```
Deps:      uv sync --extra dev
Dev:       uv run uvicorn src.main:app --reload --workers 1
Test:      uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
Lint:      uv run ruff check . --fix
Format:    uv run ruff format .
Migración: python artisan make:migration <nombre>   /   python artisan migrate
Entorno:   npx supabase start   (Supabase local; ver README)
```

### Frontend (`medicos-por-venezuela`)

```
Dev:    pnpm dev
Build:  NEXT_DIST_DIR=.next-e2e pnpm build
Types:  pnpm exec tsc --noEmit
Lint:   pnpm lint
Format: pnpm format
E2E:    pnpm test:e2e
```

## Project Structure

Archivos que este feature toca o crea:

```
api-medicos-por-venezuela/
├── db/migrations/          → 3 migraciones nuevas (tabla, flags de catálogo, permisos RBAC)
├── src/models/             → interconsultation_request.py (nuevo); patient.py (columna nueva)
├── src/schemas/            → interconsultation_request.py (nuevo)
├── src/services/           → interconsultation_requests.py (nuevo, incluye el lock)
│                             notifications.py (2 eventos nuevos); patients.py (alta por médico)
├── src/routers/            → interconsultation_requests.py (nuevo); patients.py (endpoints de médico)
├── tests/                  → test_interconsultation_requests.py
│                             test_interconsultation_requests_concurrency.py
├── .knowledge/             → interconsultas.md (actualizar: ahora son CUATRO flujos)
└── tasks/interconsulta-asincrona/  → spec.md · plan.md · todo.md

medicos-por-venezuela/
├── lib/                    → interconsultationRequests.ts, doctorPatients.ts (nuevos)
├── pages/panel-medico/     → mis-pacientes.tsx, interconsultas.tsx (nuevos)
├── components/             → formulario de caso, selector de especialidad, bandeja
├── e2e/                    → interconsulta-asincrona.spec.ts
└── changeslog.md           → entrada al terminar (protocolo del repo)
```

## Modelo de datos

### Tabla nueva: `interconsultation_requests`

No se reutiliza `interconsultations`: esa tiene `consultation_id` **NOT NULL + UNIQUE** y
este flujo no tiene consulta. Son dos cosas distintas y deben quedar distintas.

| Columna | Tipo | Nota |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `patient_id` | uuid FK `patients(id)` | El caso. Un paciente puede tener varias solicitudes. |
| `requesting_doctor_id` | uuid | `users.id` del médico tratante. |
| `mode` | text | `specialty` o `doctor`. CHECK. |
| `specialty_id` | uuid FK `specialties(id)` | Especialidad buscada. En modo `doctor` se deriva del destinatario. |
| `target_doctor_id` | uuid null | `users.id`. Solo en modo `doctor`. CHECK: NOT NULL sii `mode='doctor'`. |
| `chief_complaint` | text | Motivo/resumen clínico del caso. Obligatorio. |
| `clinical_notes` | text null | Antecedentes, estudios, lo que el tratante quiera sumar. |
| `status` | text | `open`, `taken`, `closed` o `cancelled`. CHECK. Default `open`. |
| `taken_by_doctor_id` | uuid null | `users.id` del especialista que ganó. |
| `taken_at` | timestamptz null | |
| `closed_at` | timestamptz null | Lo fija el **médico tratante**, nunca el especialista. |
| `closing_note` | text null | Opcional, al cerrar. Alimenta el historial de la próxima iteración. |
| `cancelled_at` | timestamptz null | |
| `notified_count` | int | Cuántos correos se enviaron en el fan-out (auditoría/diagnóstico). |
| `created_at` / `updated_at` | timestamptz | |

Índices: `(status, specialty_id)` para la bandeja, `(requesting_doctor_id)`,
`(taken_by_doctor_id)`, `(target_doctor_id) WHERE target_doctor_id IS NOT NULL`.

#### Máquina de estados

```
                    (tratante cancela)
        ┌──────────────────────────────────► cancelled
        │
      open ─────────────────────────────► taken ──────────────────────────► closed
             (especialista TOMA:                    (el MÉDICO TRATANTE
              carrera, gana uno)                     cierra, nunca el
                                                     especialista)
```

- `open` no expira ni se re-difunde: **queda esperando hasta que alguien la tome**.
- Solo **un** especialista por caso. De ahí la carrera y el 409.
- El especialista **no puede** cerrar ni soltar un caso que tomó. Cerrar es del tratante,
  que es quien sabe si la ayuda le sirvió.
- `cancelled` solo aplica desde `open`; `closed` solo desde `taken`. Ambos son terminales.

### `patients`: alta por médico

- Columna nueva `created_by_doctor_id uuid NULL` (`users.id`). NULL = alta pública (flujo actual).
- `phone_whatsapp` y `affected_zone` pasan a **nullable**, protegidos por un CHECK que
  preserva la garantía del flujo público:

  ```sql
  CHECK (created_by_doctor_id IS NOT NULL
         OR (phone_whatsapp IS NOT NULL AND affected_zone IS NOT NULL))
  ```

  Un paciente de consultorio no tiene "zona afectada" ni hace falta su WhatsApp — el
  especialista nunca lo va a contactar.
- `consent` **se sigue exigiendo `true`**: el médico atestigua que el paciente autorizó
  compartir su caso.
- El soft delete existente (`deleted_at`) aplica igual.

### `specialties`: excluir medicina general

Columna nueva `available_for_interconsultation boolean NOT NULL DEFAULT true`, puesta en
`false` para "Medicina general" en la misma migración.

**No se hardcodea el nombre en el código.** La migración `20260813_142814` estableció ese
precedente para salud mental por una razón explícita: un rename del catálogo rompería la
regla en silencio. El select de especialidades filtra por el flag, no por texto.

### RBAC

Dos permisos nuevos, sembrados en migración (nunca a mano), mapeados al rol `doctor`:

- `interconsultation_requests.write` → crear y cancelar la propia solicitud.
- `interconsultation_requests.take` → ver la bandeja y tomar un caso.

## Contratos HTTP (`/api/v1`)

### Pacientes del médico

| Método | Ruta | Permiso | Qué hace |
|---|---|---|---|
| `POST` | `/doctors/me/patients` | `interconsultation_requests.write` | Alta de paciente propio (`created_by_doctor_id` = caller). Exige `consent=true`. |
| `GET` | `/doctors/me/patients` | idem | Lista los pacientes que registró el caller (excluye `deleted_at`). |
| `PATCH` | `/doctors/me/patients/{id}` | idem | Edita. 403 si no es suyo. |
| `DELETE` | `/doctors/me/patients/{id}` | idem | Soft delete. 403 si no es suyo. |

No se toca el `POST /patients` público ni `GET /patients/me` (portal del paciente).

### Solicitudes de interconsulta

| Método | Ruta | Permiso | Qué hace |
|---|---|---|---|
| `POST` | `/interconsultation-requests` | `...write` | Crea la solicitud. 422 si la especialidad no está disponible para interconsulta. Dispara el fan-out por correo (BackgroundTasks). |
| `GET` | `/interconsultation-requests/mine` | `...write` | Mis solicitudes como tratante, con estado y —si fue tomada— identidad y contacto del especialista. |
| `GET` | `/interconsultation-requests/inbox` | `...take` | Solicitudes **abiertas** visibles para mí: las de mi especialidad + las dirigidas a mí. **Anonimizadas.** |
| `POST` | `/interconsultation-requests/{id}/take` | `...take` | Toma el caso. **409** si otro ganó la carrera o si ya no está `open`. Devuelve el contacto del tratante. |
| `GET` | `/interconsultation-requests/{id}` | según viewer | Detalle. La forma depende de quién mira (ver Frontera de datos). |
| `POST` | `/interconsultation-requests/{id}/cancel` | `...write` | El tratante cancela mientras siga `open`. 409 si ya fue tomada. |
| `POST` | `/interconsultation-requests/{id}/close` | `...write` | El **tratante** cierra una solicitud `taken` (nota opcional). 403 si lo intenta el especialista; 409 si no está `taken`. |

### Frontera de datos (la restricción dura)

| Viewer | Ve |
|---|---|
| **Especialista, antes de tomar** | Especialidad solicitada, `chief_complaint`, `clinical_notes`, `age_range` del paciente, `created_at`. **Nada más.** |
| **Especialista, después de tomar** | Lo anterior + nombre, WhatsApp y correo del **médico tratante**. |
| **Médico tratante** | Todo lo suyo + identidad/contacto del especialista que tomó. |

**Nunca, en ningún estado, se expone al especialista**: `full_name`, `cedula`,
`phone_whatsapp`, `email`, `affected_zone` ni `description` del paciente. El paciente no es
usuario de la plataforma y la relación con él la mantiene exclusivamente el tratante.

Antes de tomar, tampoco se expone la identidad del **médico tratante** (evita que se elija
por quién pregunta en vez de por el caso).

### Concurrencia

`take` replica el patrón ya probado en `src/services/queue.py` — **no se reinventa**:

```python
select(InterconsultationRequest)
    .where(InterconsultationRequest.id == request_id,
           InterconsultationRequest.status == "open")
    .with_for_update(nowait=True)
```

Si la fila ya está bloqueada, PostgreSQL falla de inmediato (`55P03`), el manejador global
lo traduce a **409** y la petición **no se cuelga**. Si la fila existe pero ya no está
`open`, también 409.

## Correos

Dos eventos nuevos en `NOTIFICATION_EVENTS` (`src/services/notifications.py`), canal `email`:

- `interconsultation_request_broadcast` → al crear. Destinatarios:
  - modo `specialty`: médicos con `users.specialty_id` = la pedida, `active`, `verified`,
    excluyendo al solicitante;
  - modo `doctor`: solo el destinatario.

  El correo lleva el motivo del caso y un enlace a la bandeja. **Sin PII del paciente.**
- `interconsultation_request_taken` → al tomar, al médico tratante, con el nombre del
  especialista.

Ambos respetan `should_send` (opt-out por preferencias) y son **best-effort**: un fallo de
correo nunca revierte la creación ni la toma.

## Code Style

Routers delgados, la lógica en el servicio, excepciones de dominio (nunca `HTTPException`
en services):

```python
@router.post(
    "/{request_id}/take",
    response_model=InterconsultationRequestTaken,
    summary="Tomar una solicitud de interconsulta",
    responses={
        404: {"description": "Solicitud no encontrada."},
        409: {"description": "Otro especialista ya la tomó (o ya no está abierta)."},
    },
)
async def take_request(
    request_id: uuid.UUID,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_permission("interconsultation_requests.take")),
) -> InterconsultationRequestTaken:
    """Asigna la solicitud al especialista que llama y le devuelve el contacto del médico
    tratante. Carrera resuelta con bloqueo de fallo rápido: el perdedor recibe 409."""
    return await interconsultation_requests_service.take(
        db, request_id=request_id, doctor_id=principal.id, background=background
    )
```

Convenciones: docstrings en español, `summary=` y `responses=` en todo endpoint nuevo,
comentarios que explican **por qué** (no qué), nombres de dominio en español donde ya lo están.

## Testing Strategy

**Backend** — pytest async, savepoints por test (`tests/conftest.py`), cobertura **≥95%**.

`tests/test_interconsultation_requests.py`:

- Alta de paciente por médico: se setea `created_by_doctor_id`; sin `consent` → 400;
  `phone_whatsapp`/`affected_zone` opcionales en esta vía pero obligatorios en la pública.
- IDOR: médico A no puede leer/editar/borrar el paciente de médico B (403).
- Crear solicitud en ambos modos; especialidad con `available_for_interconsultation=false` → 422.
- **Bandeja anonimizada**: aserción explícita de que la respuesta NO contiene `full_name`,
  `cedula`, `phone_whatsapp`, `email` ni `affected_zone` del paciente, ni identidad del tratante.
- Tras tomar: aparece el contacto del tratante y sigue sin aparecer PII del paciente.
- Cancelar abierta → OK; cancelar tomada → 409; cancelar ajena → 403.
- Correos: `send_mail` mockeado; se verifica destinatarios, `notified_count`, y que un fallo
  de correo **no** revierta la operación.

`tests/test_interconsultation_requests_concurrency.py` (obligatorio por
`.claude/rules/commands.md`): dos `httpx.AsyncClient` con `asyncio.gather` sobre el mismo
`take` → exactamente **un 200 y un 409**, y `taken_by_doctor_id` = el ganador.

**Frontend** — E2E Playwright (`e2e/interconsulta-asincrona.spec.ts`): registrar paciente →
solicitar por especialidad → login como especialista → ver bandeja anonimizada → tomar →
ver contacto. No hay unit/integration en ese repo (`strict_tdd: false`).

## Boundaries

**Always**

- Autorizar por `require_permission`, nunca comparando roles a mano.
- Sembrar permisos y flags de catálogo en **migración idempotente**, nunca a mano ni como
  literal en el código.
- Registrar en `audit_log`: `interconsultation_request.created`, `.taken`, `.cancelled` y la
  revelación de contacto (patrón de `doctor.contact_viewed`).
- `await session.commit()` explícito en los servicios que escriben (`get_db` hace ROLLBACK al
  cerrar; ya costó un bug en producción con 201 y cero filas).
- Dejar `ruff check`/`format` limpios y cobertura ≥95% antes de dar nada por terminado.

**Ask first**

- Cualquier cambio a las tablas o endpoints del flujo **en vivo** existente.
- Añadir dependencias nuevas.
- Cambiar el contrato de `POST /patients` público o de `GET /patients/me`.
- Tocar la política de fan-out si una especialidad resulta tener cientos de médicos.

**Never**

- Exponer PII del paciente al especialista, en ningún estado de la solicitud.
- Escribir contra Supabase de **producción** (ni inserts de prueba).
- Hardcodear "Medicina general" (ni ningún nombre de catálogo) en el código.
- Dejar que un fallo de correo revierta o bloquee la creación o la toma.
- Hacer `select` + `update` sin `with_for_update(nowait=True)` en `take`.
- Borrado duro de pacientes.

## Success Criteria

1. Un médico registra un paciente de consultorio **sin** cargar WhatsApp ni zona afectada, y
   ese paciente **no** aparece en la cola pública ni en el listado de otro médico.
2. `POST /interconsultation-requests` en modo `specialty` genera un correo a cada médico
   activo y verificado de esa especialidad (excluyendo al solicitante) y devuelve
   `notified_count` con ese número.
3. El select de especialidades del frontend **no** ofrece "Medicina general", y la API
   responde 422 si se la manda igual.
4. La bandeja del especialista no contiene ninguno de: `full_name`, `cedula`,
   `phone_whatsapp`, `email`, `affected_zone` del paciente, ni la identidad del tratante —
   verificado por test, no por inspección visual.
5. Dos especialistas tomando el mismo caso simultáneamente → **un 200 y un 409**, sin cuelgue
   (test de concurrencia con `asyncio.gather`).
6. Al tomar, el especialista recibe nombre + WhatsApp + correo del tratante, y el tratante
   recibe el correo de "tu caso fue tomado".
7. El **especialista no puede cerrar** un caso (403) y el **tratante sí** (200); cerrar algo
   que no está `taken` responde 409.
8. El flujo en vivo existente (`POST /interconsultations`) sigue funcionando igual:
   `tests/test_interconsultations.py` pasa **sin modificaciones**.
9. Suite verde (273+ tests, 0 fallos), cobertura ≥95%, `ruff` limpio, endpoints nuevos
   visibles y documentados en Swagger.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| **Fan-out masivo**: una especialidad con cientos de médicos ⇒ cientos de envíos síncronos vía `asyncio.to_thread`, lento y con riesgo de rate limit de Mailtrap. | Enviar en lotes con concurrencia acotada, registrar `notified_count`, y medir con datos reales antes de subirlo. Si el número asusta, evaluar un solo envío con BCC. **Se decide en la fase de plan.** |
| **Solicitud que nadie toma**: queda `open` para siempre, sin aviso a nadie. | Fuera de alcance en esta iteración; queda como pregunta abierta. |
| **Cuatro flujos con nombres parecidos** (interconsulta en vivo, interconsulta asíncrona, agendar seguimiento, referir). | Actualizar `.knowledge/interconsultas.md` con la tabla de los cuatro y nombres distintos en la UI. Ya hubo confusión con tres. |
| `patients.phone_whatsapp` deja de ser NOT NULL: código existente puede asumir que siempre hay valor. | El CHECK preserva la garantía para el flujo público; auditar los usos de esa columna al implementar. |

## Decisiones cerradas (2026-08-31)

Las cuatro preguntas abiertas quedaron resueltas por el usuario:

1. **Solicitud sin tomar** → queda `open` **hasta que alguien la tome**. No expira, no se
   re-difunde, no hay aviso de "nadie respondió". El tratante puede cancelarla.
2. **Un solo especialista por caso.** Confirmado: por eso la carrera con lock y el 409.
   El modelo queda 1:1, no N:M.
3. **El especialista no cierra el caso — lo cierra el médico tratante.** El especialista
   tampoco puede soltarlo. Ver la máquina de estados: `taken → closed` es una transición
   exclusiva del tratante, porque es quien sabe si la ayuda sirvió.
4. **Historial: queda para una iteración posterior.** No entra en este alcance.

### ⚠️ Lo que sí entra pese al punto 4

El **historial** (archivo consultable de casos cerrados, con métricas y filtros) queda fuera.
Pero la lista de **casos activos que tomé** sí entra, y no es lo mismo: sin ella el
especialista pierde el contacto del médico tratante en cuanto recarga la página, y el flujo
se rompe en el paso más importante. Es una lista de trabajo, no un archivo.

`closing_note` se guarda desde ahora aunque nada la muestre todavía: cuesta una columna y
evita que el historial futuro nazca sin datos que recuperar.
