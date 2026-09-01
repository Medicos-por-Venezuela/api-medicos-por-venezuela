# Implementation Plan: Interconsulta asíncrona

> Spec: [`spec.md`](./spec.md) · Checklist: [`todo.md`](./todo.md)
> Repos: `api-medicos-por-venezuela` (rama base `dev`) y `medicos-por-venezuela` (rama base `dev_aws`).

## Overview

22 tareas en 6 fases. Las fases 1–5 son backend y terminan en un PR a `dev` que ya es un
producto funcional (verificable por Swagger y tests). La fase 6 es el frontend y va en un PR
aparte a `dev_aws`. El orden sigue el grafo de dependencias de abajo hacia arriba, pero
dentro de cada fase las tareas están agrupadas en cortes verticales (dato → servicio →
endpoint → test) para que cada checkpoint deje algo demostrable, no una capa a medias.

```
Migraciones (patients · interconsultation_requests · catálogo · RBAC)
    │
    ├── Modelos ORM ──► Schemas Pydantic
    │                        │
    │                        ├── Service: pacientes del médico ──► Router /doctors/me/patients
    │                        │
    │                        └── Service: solicitudes ──► Router /interconsultation-requests
    │                                 │                          │
    │                                 └── Correo (bcc por lotes) │
    │                                                            │
    └── Seed de permisos ───────────────────────────────────────┘
                                                                 │
                                            Cliente TS ──► Páginas del panel ──► E2E
```

## Architecture Decisions

### 1. El fan-out usa el Bulk Stream de Mailtrap + BCC en lotes

El riesgo señalado en la spec se resuelve así, y es la decisión más importante del plan.
Son dos medidas que se complementan: el **stream correcto** y **menos peticiones**.

**Stream bulk.** El SDK ya instalado (`mailtrap 2.6.1`) lo soporta con un flag, sin
dependencia nueva ni cliente HTTP propio:

```python
mt.MailtrapClient(token=settings.MAILTRAP_API_TOKEN, bulk=True)  # → bulk.api.mailtrap.io
```

En `client.py::_sending_api_host` el orden es `api_host → sandbox → bulk → general`, así que
**sandbox tiene prioridad sobre bulk**: local y tests siguen entregando al inbox de pruebas
sin cambiar nada. El stream transaccional queda reservado para los correos individuales
("tu caso fue tomado"), que es donde importa la prioridad de entrega.

**BCC en lotes.** Aun con el stream bulk conviene no hacer una petición por médico:

- Se filtran los destinatarios **en SQL**: `users.specialty_id` = la pedida, `active`,
  `verified`, `email is not null`, con ficha en `doctors` (`verified`, `status = 1`,
  `deleted_at is null`), excluyendo al solicitante.
- Se aplica `notifications.should_send(prefs, evento, "email")` por usuario (respeta el opt-out).
- Los sobrevivientes van en **lotes de 50 por BCC** (`BaseMail.bcc`, ya en el SDK):
  300 médicos = 6 peticiones, no 300.
- **BCC y no `to:` múltiple**, aunque el bulk stream lo permita: los correos de los médicos
  son datos de colegas y no deben quedar expuestos en la cabecera del resto.
- Tope duro por `settings.INTERCONSULTATION_FANOUT_MAX` (default 500). Si se supera, se
  notifica hasta el tope y se loguea el recorte: **nunca un truncamiento silencioso**.
- `notified_count` guarda el número real de notificados.

El correo del fan-out no lleva PII ni nada personalizado, así que BCC no pierde nada.

**MCP no interviene.** Un servidor MCP conecta herramientas al agente durante una sesión de
desarrollo; estos correos los envía el proceso de FastAPI en runtime. Para inspeccionar
envíos en local ya está Inbucket (`http://localhost:54324`).

### 2. Tabla nueva, no extender `interconsultations`

Ya justificado en la spec (`consultation_id` NOT NULL + UNIQUE). Consecuencia operativa: el
flujo en vivo **no se toca**, y `tests/test_interconsultations.py` debe pasar sin editarse —
es la red de seguridad de que no hubo regresión.

### 3. Los routers se auto-descubren

`src/routers/__init__.py` monta solo cualquier módulo con un `router`. No hay que editar
`main.py` ni el `__init__`. Sí hay que sumar el prefijo nuevo a `_EXPECTED_PREFIXES` en
`tests/test_router_discovery.py` para que un borrado accidental se detecte.

### 4. Frontend en paralelo, contra el contrato congelado

La spec fija los contratos HTTP. Las tareas de frontend (fase 6) pueden empezar apenas
terminen las tareas 10 y 13 (que son las que congelan las respuestas reales), sin esperar a
las fases de cierre.

### 5. Permisos nuevos, no reutilizar los de la cola

`queue.take` es de la cola pública; mezclarlos daría acceso cruzado. Van dos permisos
propios mapeados al rol `doctor`, sembrados en migración siguiendo
`20260720_105744_seed_stats_read_permission.sql`.

---

## Task List

### Fase 1: Fundación de datos (API)

#### Task 1: Migración — `patients` acepta alta por médico

**Description:** Añade `created_by_doctor_id` y relaja `phone_whatsapp` / `affected_zone`
a nullable, protegidos por un CHECK que preserva la garantía del alta pública.

**Acceptance criteria:**
- [ ] `created_by_doctor_id uuid NULL` existe, con índice, y el modelo ORM lo refleja.
- [ ] Insertar un paciente sin `phone_whatsapp` y sin `created_by_doctor_id` **falla** por CHECK.
- [ ] La migración es idempotente (se puede correr dos veces).

**Verification:** `python artisan migrate` sobre base nueva y sobre base con datos ·
`uv run pytest tests/test_patients.py`

**Dependencies:** Ninguna · **Files:** `db/migrations/*_patients_alta_por_medico.sql`,
`src/models/patient.py` · **Scope:** S

---

#### Task 2: Migración + modelo — `interconsultation_requests`

**Description:** Crea la tabla con sus CHECKs (`mode`, `status`, `target_doctor_id` NOT NULL
sii `mode='doctor'`) e índices, y el modelo ORM.

**Acceptance criteria:**
- [ ] Tabla creada con todas las columnas de la spec y los 4 índices.
- [ ] `status` acepta exactamente `open`, `taken`, `closed`, `cancelled` — los cuatro
      estados de la máquina, con `closed_at` y `closing_note`.
- [ ] `mode='doctor'` sin `target_doctor_id` es rechazado por la BD, no solo por Pydantic.
- [ ] RLS deny-all como el resto de tablas del backend.

**Verification:** `python artisan migrate` · test que inserta filas inválidas y espera error

**Dependencies:** Ninguna · **Files:** `db/migrations/*_create_interconsultation_requests.sql`,
`src/models/interconsultation_request.py` · **Scope:** S

---

#### Task 3: Migración — catálogo excluye Medicina general

**Description:** Añade `specialties.available_for_interconsultation` (default `true`) y lo
pone en `false` para Medicina general. Expone el flag en el listado público de especialidades.

**Acceptance criteria:**
- [ ] La columna existe y Medicina general queda en `false`.
- [ ] `GET /specialties` devuelve el flag; hay filtro `?for_interconsultation=true`.
- [ ] El nombre "Medicina general" **no** aparece como literal en ningún `.py`.

**Verification:** `uv run pytest tests/test_specialties.py` ·
`grep -rn "Medicina general" src/` no devuelve nada

**Dependencies:** Ninguna · **Files:** `db/migrations/*_specialties_interconsultation_flag.sql`,
`src/models/specialty.py`, `src/schemas/specialty.py`, `src/services/specialties.py`,
`src/routers/specialties.py` · **Scope:** M

---

#### Task 4: Migración — seed de permisos RBAC

**Description:** Siembra `interconsultation_requests.write` y `.take` y los mapea a `doctor`,
`admin` y `super_admin` (el cross-join original ya corrió; los permisos nuevos necesitan
mapeo explícito).

**Acceptance criteria:**
- [ ] Ambos permisos existen y están mapeados a los tres roles.
- [ ] Idempotente.

**Verification:** `uv run pytest tests/test_rbac.py`

**Dependencies:** Ninguna · **Files:** `db/migrations/*_seed_interconsultation_requests_permissions.sql`
· **Scope:** XS

---

### ✅ Checkpoint 1 — Fundación

- [ ] `python artisan migrate` limpio en base nueva **y** sobre base con datos restaurados.
- [ ] Suite completa verde: **nada de lo existente se rompió** (273+ tests, 0 fallos).
- [ ] `uv run ruff check .` y `format` limpios.

---

### Fase 2: El médico registra su paciente (corte vertical A)

#### Task 5: Service + schemas — pacientes del médico

**Description:** Alta, listado, edición y soft-delete de pacientes propios, con la validación
de pertenencia (`created_by_doctor_id` = caller) y `consent=true` obligatorio.

**Acceptance criteria:**
- [ ] `create_doctor_patient` setea el dueño y rechaza `consent=false` con `BadRequestError`.
- [ ] Las lecturas y escrituras sobre un paciente ajeno lanzan `ForbiddenError`.
- [ ] El listado excluye `deleted_at` y no mezcla pacientes de altas públicas.

**Verification:** `uv run pytest tests/test_patients.py -k doctor`

**Dependencies:** Task 1 · **Files:** `src/services/patients.py`, `src/schemas/patient.py` · **Scope:** M

---

#### Task 6: Router `/doctors/me/patients` + tests

**Description:** Los cuatro endpoints, protegidos por `interconsultation_requests.write`, con
`summary`/docstring/`responses` para Swagger.

**Acceptance criteria:**
- [ ] Los 4 endpoints responden los códigos de la spec (201/200/200/204, 403 en ajeno).
- [ ] **IDOR cubierto por test**: médico A no puede leer, editar ni borrar el paciente de B.
- [ ] `POST /patients` público y `GET /patients/me` siguen intactos.

**Verification:** `uv run pytest tests/test_patients.py` · Swagger muestra el grupo nuevo

**Dependencies:** Task 5 · **Files:** `src/routers/patients.py`, `tests/test_patients.py` · **Scope:** M

---

### ✅ Checkpoint 2 — Paciente de consultorio

- [ ] Un médico da de alta un paciente sin WhatsApp ni zona afectada, vía Swagger.
- [ ] Ese paciente **no** aparece en la cola pública ni para otro médico.
- [ ] Cobertura ≥95% en los archivos tocados.

---

### Fase 3: Solicitar la interconsulta y difundirla (corte vertical B)

#### Task 7: `mail` gana stream bulk + BCC por lotes

**Description:** `_client()` acepta `bulk: bool` (pasa `bulk=True` al SDK, que resuelve a
`bulk.api.mailtrap.io`); `send_mail` acepta `bcc: list[str] | None`; y un helper `send_bulk`
trocea en lotes de 50 respetando `INTERCONSULTATION_FANOUT_MAX`. Sigue siendo best-effort.

**Acceptance criteria:**
- [ ] `send_bulk` de 120 destinatarios hace **3** llamadas al SDK, no 120, y todas por BCC.
- [ ] Con `MAILTRAP_INBOX_ID` seteado (local/tests) el cliente sigue yendo a **sandbox**,
      no a bulk — el flag no rompe el entorno de desarrollo.
- [ ] Superar el tope recorta **y loguea** cuántos quedaron fuera.
- [ ] Un fallo de un lote no aborta los demás ni lanza al caller.
- [ ] Los correos individuales existentes (recordatorios, agenda) siguen por el stream
      transaccional: `test_mail.py` pasa sin editarse.

**Verification:** `uv run pytest tests/test_mail.py`

**Dependencies:** Ninguna · **Files:** `src/services/mail.py`, `src/core/config.py`,
`tests/test_mail.py` · **Scope:** S

---

#### Task 8: Eventos de notificación del feature

**Description:** `interconsultation_request_broadcast` e `interconsultation_request_taken` en
`NOTIFICATION_EVENTS`, con el armado de asunto/cuerpo. Sin PII del paciente en el correo.

**Acceptance criteria:**
- [ ] Ambos eventos declarados con canal `email` y respetando `should_send`.
- [ ] Test que asegura que el cuerpo del correo **no** contiene datos del paciente.
- [ ] El frontend puede listarlos en preferencias (`GET /notification-prefs`).

**Verification:** `uv run pytest tests/test_notification_prefs.py tests/test_mail.py`

**Dependencies:** Task 7 · **Files:** `src/services/notifications.py`,
`tests/test_notification_prefs.py` · **Scope:** S

---

#### Task 9: Service — crear solicitud y disparar el fan-out

**Description:** Crea la solicitud en ambos modos, valida que la especialidad esté disponible
para interconsulta, resuelve los destinatarios, encola el envío y escribe `audit_log`.

**Acceptance criteria:**
- [ ] Modo `specialty`: destinatarios = médicos activos, verificados, con ficha válida, de esa
      especialidad, excluyendo al solicitante. `notified_count` refleja el número real.
- [ ] Modo `doctor`: un único destinatario; la especialidad se deriva de él.
- [ ] Especialidad con `available_for_interconsultation=false` → `UnprocessableError` (422).
- [ ] El paciente debe ser del solicitante, o `ForbiddenError`.
- [ ] `await session.commit()` explícito (el bug de 201-sin-fila no se repite).

**Verification:** `uv run pytest tests/test_interconsultation_requests.py -k create`

**Dependencies:** Tasks 2, 3, 5, 8 · **Files:** `src/services/interconsultation_requests.py`,
`src/schemas/interconsultation_request.py` · **Scope:** M

---

#### Task 10: Router — `POST /interconsultation-requests` y `GET /mine`

**Description:** Creación (con `BackgroundTasks` para el correo) y listado del tratante.
**Congela el contrato** que consume el frontend.

**Acceptance criteria:**
- [ ] `POST` responde 201 con el id y `notified_count`; 422 en especialidad no elegible.
- [ ] `GET /mine` muestra estado y, si fue tomada, identidad y contacto del especialista.
- [ ] Un fallo de correo **no** cambia el 201.

**Verification:** `uv run pytest tests/test_interconsultation_requests.py` · Swagger

**Dependencies:** Task 9 · **Files:** `src/routers/interconsultation_requests.py`,
`tests/test_interconsultation_requests.py` · **Scope:** M

---

### ✅ Checkpoint 3 — Solicitud difundida

- [ ] Vía Swagger: crear una solicitud dispara los correos esperados (Inbucket local los atrapa).
- [ ] `notified_count` coincide con los destinatarios elegibles.
- [ ] **El frontend ya puede empezar la fase 6 en paralelo.**

---

### Fase 4: Bandeja anonimizada y toma del caso (corte vertical C)

#### Task 11: Service + schemas — bandeja anonimizada

**Description:** `inbox` devuelve solicitudes abiertas de mi especialidad y las dirigidas a
mí. El schema de respuesta es el que **impone** la frontera de datos: si un campo prohibido
no está en el modelo Pydantic, no puede escaparse.

**Acceptance criteria:**
- [ ] La respuesta solo contiene: especialidad, `chief_complaint`, `clinical_notes`,
      `age_range`, `created_at`, `id`.
- [ ] No expone identidad del paciente **ni del médico tratante**.
- [ ] No muestra solicitudes ya tomadas ni canceladas.

**Verification:** `uv run pytest tests/test_interconsultation_requests.py -k inbox`

**Dependencies:** Task 9 · **Files:** `src/services/interconsultation_requests.py`,
`src/schemas/interconsultation_request.py` · **Scope:** M

---

#### Task 12: Service — `take` con bloqueo de fallo rápido

**Description:** Toma el caso con `with_for_update(nowait=True)` sobre la fila `open`, marca
`taken_by_doctor_id`/`taken_at`, devuelve el contacto del tratante, audita la revelación y
encola el correo al tratante.

**Acceptance criteria:**
- [ ] Fila bloqueada por otra transacción → error de lock 55P03 → **409**, sin cuelgue.
- [ ] Fila ya no `open` → 409. Fila inexistente → 404.
- [ ] Solo especialistas elegibles (misma especialidad, o destinatario en modo `doctor`) pueden tomar.
- [ ] `audit_log` registra `.taken` y la revelación de contacto.

**Verification:** `uv run pytest tests/test_interconsultation_requests.py -k take`

**Dependencies:** Task 11 · **Files:** `src/services/interconsultation_requests.py` · **Scope:** M

---

#### Task 13: Router — `inbox`, `take`, detalle + tests de frontera

**Description:** Los tres endpoints, con el detalle cambiando de forma según el viewer.

**Acceptance criteria:**
- [ ] Test que recorre el JSON crudo de `inbox` y del detalle pre-toma y **falla si aparece**
      `full_name`, `cedula`, `phone_whatsapp`, `email` o `affected_zone` del paciente.
- [ ] Post-toma: aparece el contacto del tratante y **sigue** sin aparecer PII del paciente.
- [ ] Un médico ajeno a la especialidad recibe 403 al intentar tomar.

**Verification:** `uv run pytest tests/test_interconsultation_requests.py` · Swagger

**Dependencies:** Task 12 · **Files:** `src/routers/interconsultation_requests.py`,
`tests/test_interconsultation_requests.py` · **Scope:** M

---

#### Task 14: Test de concurrencia (obligatorio por las reglas del repo)

**Description:** Dos clientes tomando el mismo caso a la vez, con `asyncio.gather`, calcado
de `tests/test_queue_concurrency.py`.

**Acceptance criteria:**
- [ ] Exactamente **un 200 y un 409**. Nunca dos ganadores, nunca dos perdedores.
- [ ] `taken_by_doctor_id` es el del que recibió 200.
- [ ] Ninguna petición se cuelga.

**Verification:** `uv run pytest tests/test_interconsultation_requests_concurrency.py`

**Dependencies:** Task 13 · **Files:** `tests/test_interconsultation_requests_concurrency.py` · **Scope:** S

---

### ✅ Checkpoint 4 — Flujo backend completo

- [ ] End-to-end por Swagger: registrar paciente → solicitar → tomar → ver contacto.
- [ ] Ambos correos llegan a Inbucket.
- [ ] El test de concurrencia pasa 10 corridas seguidas sin flakear.

---

### Fase 5: Cierre del backend

#### Task 15: `cancel` y `close` + tests

**Description:** Las dos transiciones terminales, ambas **exclusivas del médico tratante**.
`cancel` sale de `open`; `close` sale de `taken` con `closing_note` opcional.

**Acceptance criteria:**
- [ ] El tratante cancela una `open` → 200. Cancelar una `taken` → 409. Ajena → 403.
- [ ] El tratante cierra una `taken` → 200, setea `closed_at`. Cerrar una `open` → 409.
- [ ] **El especialista que tomó el caso NO puede cerrarlo** → 403. Test explícito: es la
      regla que el usuario pidió y la que un refactor futuro rompería sin querer.
- [ ] Ambas quedan auditadas (`.cancelled`, `.closed`).

**Verification:** `uv run pytest tests/test_interconsultation_requests.py -k "cancel or close"`

**Dependencies:** Task 13 · **Files:** `src/services/`, `src/routers/`, `tests/` (3 archivos) · **Scope:** S

---

#### Task 16: Documentación y red de seguridad

**Description:** `.knowledge/interconsultas.md` pasa a documentar **cuatro** flujos, con la
tabla comparativa; se suma el prefijo nuevo a `_EXPECTED_PREFIXES`.

**Acceptance criteria:**
- [ ] La tabla distingue los cuatro flujos y dice cuál usar cuándo.
- [ ] `tests/test_router_discovery.py` incluye `/interconsultation-requests`.
- [ ] Swagger: todos los endpoints nuevos con `summary`, docstring y `responses`.

**Verification:** `uv run pytest tests/test_router_discovery.py` · revisar `/docs`

**Dependencies:** Task 15 · **Files:** `.knowledge/interconsultas.md`,
`tests/test_router_discovery.py` · **Scope:** S

---

### ✅ Checkpoint 5 — Backend listo para PR

- [ ] Suite **verde, 0 fallos**; cobertura **≥95%**; `ruff check`/`format` limpios.
- [ ] `tests/test_interconsultations.py` pasa **sin haberse editado** (cero regresión en el flujo en vivo).
- [ ] PR contra `dev`.

---

### Fase 6: Frontend (`medicos-por-venezuela`, rama base `dev_aws`)

#### Task 17: Clientes TypeScript

**Acceptance criteria:**
- [ ] `lib/doctorPatients.ts` y `lib/interconsultationRequests.ts` con los tipos de la spec.
- [ ] Los tipos de la vista anonimizada **no declaran** campos de PII del paciente.
- [ ] `pnpm exec tsc --noEmit` limpio.

**Dependencies:** Tasks 10, 13 · **Files:** 2 archivos en `lib/` · **Scope:** S

---

#### Task 18: `pages/panel-medico/mis-pacientes.tsx`

**Acceptance criteria:**
- [ ] Formulario corto (nombre, edad/rango, alergias, notas) + checkbox de consentimiento
      obligatorio; no pide WhatsApp ni zona afectada.
- [ ] Listado con edición y borrado de los propios.
- [ ] Enlazada desde el panel médico.

**Verification:** `pnpm lint` · `pnpm exec tsc --noEmit` · QA manual

**Dependencies:** Task 17 · **Files:** 1 página + 1–2 componentes · **Scope:** M

---

#### Task 19: Solicitar interconsulta

**Acceptance criteria:**
- [ ] El select de especialidad **no ofrece Medicina general** (viene filtrado de la API).
- [ ] "Por especialidad" es la opción por defecto; "médico específico" es secundaria.
- [ ] En modo médico específico se muestra su teléfono.
- [ ] Confirmación con `notified_count` ("se notificó a N especialistas").

**Verification:** `pnpm lint` · `pnpm exec tsc --noEmit` · QA manual

**Dependencies:** Task 17 · **Files:** 1 página + 2 componentes · **Scope:** M

---

#### Task 20: Bandeja del especialista

**Acceptance criteria:**
- [ ] Lista anonimizada + botón Tomar; el 409 se muestra como "otro colega ya lo tomó" y
      refresca la lista, no como error genérico.
- [ ] Pestaña **"casos activos que tomé"** con el contacto del tratante (WhatsApp
      clickeable). No es el historial (eso queda para otra iteración): sin esta lista el
      especialista pierde el contacto al recargar.
- [ ] "Mis solicitudes" para el tratante, con estado, cancelar (si `open`) y **cerrar**
      (si `taken`, con nota opcional).
- [ ] El botón de cerrar **no aparece** en la vista del especialista.

**Verification:** `pnpm lint` · `pnpm exec tsc --noEmit` · QA manual

**Dependencies:** Task 19 · **Files:** 1 página + 2–3 componentes · **Scope:** M

---

#### Task 21: E2E Playwright

**Acceptance criteria:**
- [ ] `e2e/interconsulta-asincrona.spec.ts` recorre: registrar paciente → solicitar →
      login como especialista → bandeja → tomar → ver contacto.
- [ ] Asserta que la bandeja **no** contiene el nombre del paciente.

**Verification:** `pnpm test:e2e`

**Dependencies:** Task 20 · **Files:** 1 spec + seed en `e2e/global-setup.ts` · **Scope:** S

---

#### Task 22: `changeslog.md`

**Acceptance criteria:** entrada al tope, agrupada por fecha, con qué cambió y por qué.

**Dependencies:** Task 21 · **Files:** 1 · **Scope:** XS

---

### ✅ Checkpoint 6 — Completo

- [ ] Los 9 criterios de éxito de la spec, verificados uno por uno.
- [ ] `pnpm build` (con `NEXT_DIST_DIR=.next-e2e`), `lint`, `tsc --noEmit` y `test:e2e` verdes.
- [ ] PR contra `dev_aws`.

---

## Risks and Mitigations

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Fan-out masivo satura Mailtrap o tarda minutos | **Alto** | Stream **bulk** (`bulk=True`, ya en el SDK) + BCC en lotes de 50 + tope configurable + `notified_count` (Task 7). Decidido, no diferido. |
| El flag `bulk` desvía también los correos de local/tests | Bajo | `sandbox` tiene prioridad sobre `bulk` en el SDK; cubierto por criterio de aceptación explícito en la Task 7. |
| Relajar `phone_whatsapp` a nullable rompe código que asumía valor | **Alto** | El CHECK preserva la garantía pública; auditar los usos de la columna dentro de Task 1 antes de migrar. |
| La PII se filtra por un campo agregado después sin pensar | **Alto** | La frontera vive en el **schema Pydantic** (Task 11), no en un `del` manual, y el test recorre el JSON crudo (Task 13). |
| Test de concurrencia flakea en CI | Medio | Correrlo 10 veces en el Checkpoint 4; el patrón ya está probado en `test_queue_concurrency.py`. |
| Cuatro flujos con nombres casi iguales confunden a médicos y a devs | Medio | Task 16 documenta los cuatro; la UI usa nombres distintos ("Pedir segunda opinión" vs "Interconsulta en vivo"). |
| Los dos repos avanzan a ritmos distintos y el frontend queda contra un contrato viejo | Bajo | El contrato se congela en el Checkpoint 3; la fase 6 no arranca antes. |

## Parallelization

- **Secuencial obligado:** fase 1 completa antes que todo (migraciones), y dentro de cada
  corte vertical service → router → test.
- **Paralelizable:** Tasks 1–4 entre sí (migraciones independientes); Task 7–8 en paralelo a
  Task 5–6; toda la fase 6 en paralelo a la fase 5 una vez pasado el Checkpoint 3.

## Decisiones cerradas

Las cuatro preguntas de la spec quedaron resueltas antes de empezar (2026-08-31):

1. **Sin expiración** — la solicitud queda `open` hasta que alguien la tome.
2. **Un solo especialista** por caso → el modelo 1:1 y el lock de la Task 12 se confirman.
3. **Cierra el tratante, no el especialista** → estado `closed` y endpoint `close`
   (Task 15). El especialista tampoco puede soltar el caso.
4. **Historial diferido** a otra iteración. Lo que sí entra es la lista de *casos activos que
   tomé* (Task 20), sin la cual el especialista pierde el contacto del tratante al recargar.

`closing_note` se persiste desde ahora aunque nada la lea todavía: una columna barata que
evita que el historial futuro arranque sin datos.

**Fuera de alcance, candidatos a iteración siguiente:** historial y métricas de casos
cerrados; expiración/re-difusión de solicitudes muertas; `release` de un caso tomado.
