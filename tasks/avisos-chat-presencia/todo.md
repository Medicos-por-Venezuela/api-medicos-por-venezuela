# Tareas: Avisos del chat y presencia del paciente

> Fase 3 de 3. Spec: `spec.md` · Plan: `plan.md`.
> Orden por **dependencia**, no por importancia. Ninguna tarea toca más de 5 ficheros.
> Al terminar cada rebanada: `uv run pytest --cov=src` verde y `ruff check`/`format` limpios.

## Rebanada 1 — Nombre del médico y primer contacto (R1)

- [x] **T1. Composición: el correo nombra al médico y distingue primer contacto**
  - Acceptance: `patient_message_received_email(cid, code, has_account, doctor_name,
    is_first_contact)` produce cuatro asuntos distinguibles por las dos variables; sin nombre
    cae a «Tu médico»; el nombre sale escapado en el HTML.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k correo_paciente -v`
  - Files: `src/services/notifications.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T2. Aserción negativa de PII sobre el correo nuevo**
  - Acceptance: dado un hilo con cuerpo clínico y un adjunto con nombre, ni el texto ni el HTML
    del aviso contienen el cuerpo ni el nombre del archivo. Falla si alguien los añade.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k pii -v`
  - Files: `tests/test_avisos_chat_presencia.py`
  - Nota: va **antes** del wiring. Escrita después, confirmaría lo que el código ya hace;
    escrita ahora, defiende la decisión frente al próximo que quiera «dar más contexto».

- [x] **T3. `message_received_mail_args` resuelve nombre y primer contacto**
  - Acceptance: resuelve `doctor_name` con `_doctor_name` y `is_first_contact` con un COUNT de
    `patient_to_doctor`; ambos valores planos en el dict; sin médico asignado no revienta.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k args -v`
  - Files: `src/services/notifications.py`, `tests/test_avisos_chat_presencia.py`

## Rebanada 2 — Presencia persistente y por cadena (R2)

- [x] **T4. Setting del throttle de persistencia**
  - Acceptance: `MESSAGING_PRESENCE_PERSIST_SECONDS: int = 30` en `Settings`, documentado en
    `.env.example` con el porqué (CDC + 25 UPDATE/s).
  - Verify: `uv run pytest tests/test_config.py -v`
  - Files: `src/core/config.py`, `.env.example`

- [x] **T5. `touch_patient_presence` con throttle**
  - Acceptance: escribe memoria siempre; escribe `patient_last_seen_at` solo si pasaron
    `MESSAGING_PRESENCE_PERSIST_SECONDS` o la columna está en `null`. Cero consultas extra (usa
    la fila ya cargada).
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k presencia_throttle -v`
  - Files: `src/services/messaging.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T6. Las cuatro señales de `messaging` usan el helper**
  - Acceptance: `send_message`, `list_messages`, `mark_as_read` y la subida/descarga de
    adjuntos pasan por `touch_patient_presence`. Ninguna escribe `patient_last_seen_at` a mano.
  - Verify: `uv run pytest tests/test_mensajeria.py tests/test_avisos_chat_presencia.py -v`
  - Files: `src/services/messaging.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T7. `sse_events` recibe `touch_presence` inyectado**
  - Acceptance: la función sigue sin tocar la base; con un doble, se la llama una vez por
    ciclo; si no se pasa, el stream funciona igual (compatibilidad con los tests existentes).
  - Verify: `uv run pytest tests/test_waiting_room.py -v`
  - Files: `src/services/waiting_room.py`, `tests/test_waiting_room.py`

- [x] **T8. El router registra la presencia contra el caso VIGENTE**
  - Acceptance: el stream de un paciente derivado marca presencia en la consulta **hija**; el
    buzón del especialista lo ve `patient_online=true`. Con el `dict` vaciado (simula reinicio),
    `patient_last_seen_at` sostiene el veredicto.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k derivad -v`
  - Files: `src/routers/consultations.py`, `tests/test_avisos_chat_presencia.py`
  - Nota: es el bug que no se ve en desarrollo — hacen falta dos consultas y dos médicos.

## Rebanada 3 — Anti-ráfaga en base de datos (R4)

- [x] **T9. Migración: tres columnas en `consultations`**
  - Acceptance: `patient_notice_sent_at`, `doctor_notice_sent_at`, `patient_notice_pending_at`
    (todas `timestamptz` nullable). Idempotente (`if not exists`) y transaccional. Sin backfill.
  - Verify: `python artisan migrate && python artisan migrate:status`
  - Files: `db/migrations/2026…_avisos_chat_presencia.sql`, `src/models/consultation.py`

- [x] **T10. `should_debounce_mail` con una sola regla, desde la base**
  - Acceptance: los cuatro casos — primer aviso envía; segundo dentro de la ventana frena;
    pasada la ventana envía; tras `mark_as_read` envía. Sin leer memoria de proceso.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k debounce -v`
  - Files: `src/services/notifications.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T11. Retirar `_LAST_MAIL_NOTIFICATIONS` y sus dos funciones**
  - Acceptance: `grep -rn "_LAST_MAIL_NOTIFICATIONS\|record_mail_debounce\|clear_mail_debounce"
    src` no devuelve nada. `mark_as_read` limpia la columna del lector **dentro** de su
    transacción, no después del commit.
  - Verify: `uv run pytest tests/test_mensajeria.py -v`
  - Files: `src/services/notifications.py`, `src/services/messaging.py`

## Rebanada 4 — Diferir el aviso al paciente en línea (R3)

- [x] **T12. Settings del diferido**
  - Acceptance: `MESSAGING_MAIL_ONLINE_DELAY_SECONDS: int = 90` en `Settings` y `.env.example`.
  - Verify: `uv run pytest tests/test_config.py -v`
  - Files: `src/core/config.py`, `.env.example`

- [x] **T13. Paciente en línea ⇒ se difiere en vez de enviar**
  - Acceptance: con presencia fresca, `POST /messages` responde 201, **no** encola correo y deja
    `patient_notice_pending_at` puesto, con log `MAIL:deferred`. Sin presencia, correo inmediato
    y pending en `null` (sin regresión).
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k diferido -v`
  - Files: `src/services/notifications.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T14. Leer cancela el aviso diferido**
  - Acceptance: `POST /messages/read` del paciente pone `patient_notice_pending_at` en `null`;
    el barrido posterior no manda nada.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k cancela -v`
  - Files: `src/services/messaging.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T15. `send_pending_message_notices`: barrido de un solo intento**
  - Acceptance: manda los pending vencidos que siguen sin leer; limpia el pending **antes** del
    intento (dos pasadas solapadas no duplican); no manda los que ya se leyeron ni los que aún
    no vencieron; devuelve cuántos aceptó Mailtrap.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k barrido -v`
  - Files: `src/services/notifications.py`, `tests/test_avisos_chat_presencia.py`

- [x] **T16. Endpoint del barrido para el cron externo**
  - Acceptance: `POST /consultations/messages/send-pending-notices` con `queue.manage`, con
    `summary`, docstring y `responses` en Swagger. Devuelve `{sent, delay_seconds}`.
  - Verify: `uv run pytest tests/test_avisos_chat_presencia.py -k endpoint -v`
  - Files: `src/routers/consultations.py`, `src/schemas/consultation.py`

## Cierre

- [x] **T17. Definition of Done**
  - Acceptance: suite verde, cobertura ≥95 %, `ruff check . --fix` y `ruff format .` limpios,
    migración aplicada, `.env.example` al día y los nuevos endpoints visibles en `/docs`.
  - Verify: `uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto`

## Cierre de la implementación (2026-10-16)

Las 17 tareas están hechas. Suite: **877 pasan**, cobertura total **98 %**
(`presence.py` 100 %, `registration_mail.py` 100 %, `notifications.py` 95 %), `ruff` limpio y la
migración aplicada. Cuatro cosas que aparecieron al implementar y no estaban en el plan:

- **Bug propio, encontrado por un test que falló: faltaba el `commit`.** `message_received_mail_args`
  corre en el router DESPUÉS de que `messaging.send_message` cerró su transacción, y lo que viene
  detrás es un `BackgroundTask`. Marcar las columnas nuevas sin commitear dejaba la marca en la
  sesión, `get_db` la cerraba sin escribir y **ni el anti-ráfaga ni el diferido existían**: habría
  pasado la revisión entera comportándose exactamente como el código viejo, en silencio. Lo cazó
  `test_diferido_paciente_en_linea_no_recibe_correo`, que asierta la columna y no solo el correo —
  un test que solo contara correos habría pasado.
- **La presencia tuvo que salir a su propio módulo** (`services/presence.py`). `notifications`
  necesita leerla para diferir y `messaging` ya importa `notifications`: dejarla donde estaba
  cerraba un ciclo de imports. El import local que lo esquivaba habría escondido la dependencia
  en vez de resolverla. `messaging` reexporta los tres nombres para los llamantes de siempre.
- **`sse_events` recibe `touch_presence` inyectado**, como `fetch`. Meterle una sesión dentro la
  habría convertido en una función con IO, y es la única pieza del stream que hoy se prueba sin
  base ni reloj real.
- **El `_aware()` de `presence`.** Las columnas son `timestamptz`, pero un doble de test o una
  fila vieja pueden traer un naive, y comparar naive con aware lanza `TypeError` **justo** en el
  camino que decide si sale un correo. Tiene test propio
  (`test_debounce_tolera_un_timestamp_sin_zona`).

### Lo que queda fuera, a propósito

- **Redis para presencia en vivo**: sigue siendo v2. El `dict` en memoria se conserva como vía
  rápida, pero ya no es la única fuente — la columna va detrás.
- **WhatsApp**: el canal no existe (R9–R11 del spec madre). El diferido de R3 es el punto donde
  engancha cuando exista.
- **Escalada por mensaje sin leer tras N horas**: otra feature, con otra conversación de producto
  detrás (¿a quién se escala?).

### Requisito de despliegue

El cron externo tiene que llamar a `POST /consultations/messages/send-pending-notices` **cada
minuto** (ver `.knowledge/agenda.md`). Sin él solo se pierden avisos redundantes —los de pacientes
desconectados salen en el acto— y cada diferido deja un `MAIL:deferred` en los logs.
