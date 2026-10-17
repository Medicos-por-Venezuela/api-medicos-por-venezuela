# Spec: Avisos del chat y presencia del paciente

> Nace de la auditoría de los tres escenarios de notificación (2026-10-16). Amplía
> `tasks/mensajeria-medico-paciente/spec.md` (R6 «Avisos por correo» y R7 «presencia
> asimétrica»): **esto no es un módulo nuevo, son cuatro features del chat** que corrigen lo que
> la implementación de R6/R7 dejó a medias.
> Fases posteriores: `plan.md` y `todo.md` en esta misma carpeta.
> Un solo repo: `api-medicos-por-venezuela`. **No toca el frontend.**

## Supuestos (corrígeme antes de que empiece)

1. **El correo del chat puede nombrar al médico sin violar la frontera de PII.** El correo de
   videoconsulta ya lo hace (`notifications.video_ready_email`) y el nombre del médico no es
   dato clínico ni PII del paciente: es quién le está atendiendo, que es justo lo que el
   paciente necesita saber para decidir si abre el correo. Si esto no es así, R1 cae entero.
2. **La presencia sigue siendo asimétrica.** Todo lo que se añade aquí mejora lo que el médico
   ve del paciente. El paciente **nunca** ve presencia del médico (CA4.3/CA7.2 del spec madre).
   Ninguna respuesta hacia el paciente gana un campo.
3. **Un aviso diferido que se pierde es mejor que un aviso de más.** El diferido solo existe
   cuando el paciente está EN LÍNEA, o sea mirando la pantalla donde el mensaje ya apareció por
   SSE. Si el barrido no corre, lo que se pierde es un correo redundante para alguien que ya
   leyó el mensaje. Esa asimetría de riesgo es la que permite que R3 sea simple.
4. **Nada retroactivo.** No se avisa de mensajes viejos sin leer. Las columnas nuevas nacen en
   `null` y solo las escriben los mensajes a partir del despliegue.
5. **Un correo caído nunca rompe el envío del mensaje.** Regla ya vigente (`mail.best_effort`).

## Objective

Que el correo del chat diga **quién** escribió y **qué pasó**, y que no le llegue a un paciente
que está leyendo el mensaje en pantalla — sin perder el aviso del que de verdad no está.

### Actores

| Actor | Qué gana |
|---|---|
| **Paciente** | Un correo que identifica a su médico por su nombre en el asunto, y que no se repite mientras está en la plataforma leyendo. |
| **Médico tratante** | Un indicador de presencia que no miente: deja de decir «Desconectado» de un paciente que lleva veinte minutos esperando. |
| **Operación** | Una regla de anti-ráfaga que sobrevive a un reinicio y a una segunda réplica, sin que nadie tenga que acordarse de que era de memoria. |

### Por qué ahora

Cuatro defectos concretos, los cuatro verificados en el código:

1. **`patient_message_received_email` no recibe el nombre del médico** (`notifications.py:635`).
   El paciente recibe «Tu médico te respondió» de alguien a quien no ha visto nunca. El dato
   existe a dos líneas de distancia: `_doctor_name()` (`notifications.py:147`), que el correo de
   videoconsulta ya usa.
2. **El asunto miente en el primer contacto.** «Tu médico te **respondió**» sale igual cuando el
   paciente nunca escribió. El primer mensaje del médico en un hilo es, para el paciente, el
   aviso de que su caso fue atendido — y el correo no lo dice.
3. **La presencia es de mentira fuera del proceso.** `record_patient_presence` escribe un `dict`
   en memoria (`messaging.py:56`); el respaldo compartido `consultations.patient_last_seen_at`
   **solo** se escribe cuando el paciente *escribe* (`messaging.py:308,447`). La señal más común
   —el SSE de la sala de espera (`waiting_room.py:151`)— no lo toca. Tras un reinicio, o en una
   segunda réplica, el buzón del médico da «Desconectado» de quien está delante de la pantalla.
   Y peor: la presencia del SSE se registra contra `requested_id`, no contra el caso vigente de
   la cadena, así que **un paciente derivado aparece desconectado para el especialista que lo
   tiene en cola**, que es exactamente el médico que necesita saberlo.
4. **El correo sale aunque el paciente esté leyendo.** `message_received_mail_args` no consulta
   presencia en ningún punto. El SSE entrega el mensaje en ≤4 s; el correo llega después, a
   alguien que ya lo leyó. Y como `mark_as_read` limpia el anti-ráfaga
   (`messaging.py:849`), una conversación de ida y vuelta con un paciente atento produce **un
   correo por cada mensaje del médico**.

### Historias de usuario

1. Como paciente, recibo un correo que dice el nombre del médico que me escribió, para saber de
   quién es antes de abrirlo.
2. Como paciente, la primera vez que mi médico me escribe el correo me dice que mi caso fue
   atendido, no que «me respondió» a algo que nunca escribí.
3. Como paciente que está leyendo el chat, **no** recibo un correo por un mensaje que acabo de
   ver en pantalla.
4. Como paciente que cerró la pestaña, sí recibo el correo, aunque estuviera conectado en el
   momento exacto en que el médico escribió.
5. Como médico, el buzón me dice «En línea» de un paciente que está esperando, aunque la API se
   haya reiniciado hace un minuto y aunque al paciente lo hayan derivado a mi cola.

## Requisitos

### R1 — El aviso de chat al paciente dice quién escribió y qué pasó

- **CA1.1** `patient_message_received_email` recibe `doctor_name: str | None`. Con nombre, el
  asunto y la primera línea lo usan tal cual (igual que `video_ready_email`); sin nombre, cae a
  «Tu médico». El nombre pasa por `mail.esc` en el HTML, como todo lo que teclea una persona.
- **CA1.2** El correo distingue **primer contacto** de **respuesta**, mirando si el hilo tiene
  algún mensaje `patient_to_doctor`:
  - sin ninguno → asunto `«{médico} te escribió por tu caso»`, cuerpo «ha revisado tu caso y te
    ha enviado un mensaje».
  - con alguno → asunto `«{médico} te respondió»`, cuerpo como hoy.
- **CA1.3** Sigue **sin texto clínico, sin cuerpo del mensaje y sin nombres de adjuntos**. El
  test negativo de PII del hilo sigue pasando sin cambios.
- **CA1.4** El correo al **médico** (`doctor_message_received_email`) no cambia: el médico sabe
  quién es su paciente y el nombre del paciente es PII que no tiene por qué salir en un asunto.

### R2 — La presencia del paciente sobrevive al reinicio y a la derivación

- **CA2.1** Toda señal de presencia escribe **las dos** vías: el registro en memoria (rápido) y
  `consultations.patient_last_seen_at` (compartido). Las cinco señales son: stream SSE de la
  sala de espera, `GET /messages`, `POST /messages/read`, `POST /messages`, `POST /attachments`.
- **CA2.2** La escritura persistente va **throttleada**: como mucho una cada
  `MESSAGING_PRESENCE_PERSIST_SECONDS` (30 s por defecto) por consulta. Sin esto, el SSE
  escribiría la fila cada `WAITING_ROOM_POLL_SECONDS` (4 s) por paciente en espera — cien
  pacientes serían 25 UPDATE/s sobre `consultations`, que además es una tabla con CDC hacia el
  Board. El umbral de «en línea» son 45 s, así que 30 s de granularidad no cambia ningún
  veredicto.
- **CA2.3** La presencia se registra contra el **caso vigente de la cadena**, no contra el id
  que pidió el paciente. Un paciente derivado está en línea para el especialista que lo tiene
  en cola, que es el único que puede hacer algo con esa información.
- **CA2.4** `is_patient_online` no cambia de contrato: memoria primero, `patient_last_seen_at`
  después, mismo TTL. Lo que cambia es que la segunda vía ahora tiene datos.
- **CA2.5** El paciente no gana ningún campo de presencia en ninguna respuesta (asimetría).

### R3 — No se le escribe por correo a un paciente que está leyendo

- **CA3.1** Al crearse un mensaje `doctor_to_patient`, si `is_patient_online` es cierto el
  correo **no sale**: se anota `consultations.patient_notice_pending_at = now()` y se devuelve
  `None`. Si es falso, sale ya (comportamiento de hoy).
- **CA3.2** `POST /messages/read` del paciente **limpia** `patient_notice_pending_at`. Un
  paciente que lee dentro de la ventana no recibe ningún correo por ese mensaje. Ese es el
  objetivo entero de R3.
- **CA3.1b** El reloj del diferido lo fija el **primer** mensaje sin entregar, no el último: si
  se reescribiera en cada mensaje, un médico que manda cinco frases seguidas a un paciente con la
  pestaña abierta pero sin leer empujaría el aviso hacia adelante indefinidamente.
- **CA3.3** `send_pending_message_notices(session)` manda los avisos cuya espera pasó de
  `MESSAGING_MAIL_ONLINE_DELAY_SECONDS` (90 s) y que **siguen** con mensajes
  `doctor_to_patient` sin leer. Limpia `patient_notice_pending_at` tras el intento —un solo
  intento, como `send_due_reminders`— y anota `patient_notice_sent_at`.
- **CA3.4** Se expone en `POST /consultations/messages/send-pending-notices`, permiso
  `queue.manage`, para el **mismo cron externo** que ya llama a `agenda/send-due-reminders`.
  Devuelve `{sent, delay_seconds}`.
- **CA3.5** El aviso al **médico** no se difiere nunca. No hay registro de presencia del médico
  —y no lo habrá, porque la asimetría de R7 es deliberada— así que no hay nada que consultar.
- **CA3.6** Si el cron no está configurado, lo que se pierde es el correo de un paciente que
  estaba mirando la pantalla. Se registra `MAIL:deferred` al diferir para que el silencio sea
  visible en los logs.

### R4 — Una sola regla de anti-ráfaga, compartida entre réplicas

- **CA4.1** Desaparece `_LAST_MAIL_NOTIFICATIONS` (`notifications.py:550`) y con él
  `record_mail_debounce` / `clear_mail_debounce`. El estado pasa a dos columnas de
  `consultations`: `patient_notice_sent_at` y `doctor_notice_sent_at`.
- **CA4.2** `should_debounce_mail` queda con **una sola** regla, no dos: «hay un aviso enviado
  a este destinatario por este hilo hace menos de `MESSAGING_MAIL_DEBOUNCE_MINUTES` **y** queda
  al menos un mensaje de esa dirección sin leer». Hoy conviven dos criterios que convergen por
  accidente aritmético —memoria usa `unread > 0`, base usa `unread >= 2`— y divergirán en
  cuanto alguien toque el orden del commit.
- **CA4.3** `mark_as_read` limpia la columna del lector (`patient_notice_sent_at` si lee el
  paciente, `doctor_notice_sent_at` si lee el médico), que es lo que hoy hace
  `clear_mail_debounce`. El comportamiento visible no cambia.
- **CA4.4** La migración es **idempotente y transaccional** (`add column if not exists`), como
  exige `.claude/rules/db_env.md`. Tres columnas nullable, ningún backfill.

## Tech Stack

Python 3.12 · FastAPI async · SQLAlchemy 2.0 (asyncpg) · Pydantic v2 · PostgreSQL 17 (Supabase)
· Mailtrap · `uv` · Ruff · pytest + pytest-asyncio.

**Sin dependencias nuevas y sin Redis.** R4 resuelve el estado compartido con columnas, que es
lo que el repo ya hace para la idempotencia del recordatorio (`reminder_sent_at`). Redis seguía
siendo la respuesta si hiciera falta un registro de presencia *en vivo* compartido; para
anti-ráfaga y diferido, que toleran la granularidad de una fila, no lo es.

## Commands

```bash
uv sync --extra dev
python artisan migrate
uv run uvicorn src.main:app --reload --workers 1
uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
uv run ruff check . --fix && uv run ruff format .
```

## Project Structure

```
db/migrations/2026…_avisos_chat_presencia.sql  → NUEVO. 3 columnas en consultations
src/models/consultation.py                     → CAMBIA: 3 campos
src/core/config.py                             → CAMBIA: 2 settings nuevos
src/services/notifications.py                  → CAMBIA: R1, R3, R4 (el grueso)
src/services/messaging.py                      → CAMBIA: R2 (presencia), R3 (limpiar pending)
src/services/waiting_room.py                   → CAMBIA: R2 (presencia desde el SSE)
src/routers/consultations.py                   → CAMBIA: wiring del SSE + endpoint del barrido
src/schemas/consultation.py                    → CAMBIA: PendingNoticesRunResponse
tests/test_avisos_chat_presencia.py            → NUEVO
```

## Code Style

El patrón del repo, sin excepciones: los datos se resuelven **dentro** de la request (sesión
viva) y se pasan planos al `BackgroundTask`; los servicios lanzan excepciones de dominio, nunca
`HTTPException`; docstrings que explican el **porqué**; nada de PII en logs.

`sse_events` recibe `touch_presence` como **callable inyectado**, igual que `fetch` y
`fetch_latest_message`. Esa función es pura a propósito —se prueba sin base ni reloj real— y
meterle una sesión dentro la convertiría en lo contrario.

## Testing Strategy

pytest + pytest-asyncio, savepoints, cobertura **≥95 %**. Los tests nunca envían:
`mail_enabled()` es `False` sin token y además se dobla `mail.send_mail`.

| Nivel | Qué cubre |
|---|---|
| Unidad | R1: con nombre y sin nombre; primer contacto vs respuesta; el nombre aparece escapado en el HTML |
| Unidad | R1: **aserción negativa de PII** — el cuerpo no contiene el texto del mensaje ni nombres de adjuntos |
| Unidad | R4: debounce con una sola regla — envía, frena dentro de la ventana, vuelve a enviar pasada la ventana, vuelve a enviar tras leer |
| Unidad | R2: el throttle no escribe dos veces dentro de `MESSAGING_PRESENCE_PERSIST_SECONDS` |
| Unidad | R2: `sse_events` llama a `touch_presence` en cada ciclo (con doble, sin base) |
| Integración | R3: paciente en línea ⇒ 201 y **cero** correos, con `patient_notice_pending_at` puesto |
| Integración | R3: paciente en línea que lee ⇒ el barrido no manda nada y limpia el pending |
| Integración | R3: paciente en línea que **no** lee ⇒ el barrido manda el correo pasada la espera |
| Integración | R3: paciente offline ⇒ correo inmediato, sin pending (no hay regresión) |
| Integración | R2: tras `GET /messages` de un paciente derivado, el buzón del especialista lo ve en línea |
| Integración | R2: con el registro en memoria vacío (simula reinicio), `patient_last_seen_at` sostiene el «en línea» |
| Resiliencia | `send_mail` lanzando: el `POST /messages` sigue devolviendo 201 |

El test de la **derivación** (R2/CA2.3) es el que hace que la decisión sobreviva: es un bug que
no se ve en ninguna pantalla de desarrollo, porque hace falta una cadena de dos consultas y dos
médicos para reproducirlo.

## Boundaries

- **Siempre:** correo best-effort; resolver datos con la sesión viva; presencia asimétrica
  (nada hacia el paciente); migración idempotente y transaccional; cobertura ≥95 %.
- **Preguntar antes:** meter el nombre del **paciente** en algún asunto; diferir también el
  aviso al médico; cambiar `MESSAGING_PATIENT_PRESENCE_TTL_SECONDS`; añadir Redis.
- **Nunca:** poner cuerpo del mensaje, texto clínico o nombres de adjuntos en un correo;
  exponer presencia del médico al paciente; reintentar un aviso diferido más de una vez;
  escribir `patient_last_seen_at` sin throttle.

## Success Criteria

1. El primer mensaje de un médico a un paciente produce un correo cuyo asunto lleva **el nombre
   del médico** y dice que revisó su caso, no que «respondió».
2. El segundo mensaje, tras una respuesta del paciente, dice «te respondió».
3. Un paciente con el chat abierto que recibe un mensaje y lo lee **no recibe ningún correo**.
4. Ese mismo paciente, si no lo lee, recibe el correo en la siguiente pasada del cron.
5. Un paciente sin conexión recibe el correo **inmediatamente**, como hoy.
6. Reiniciar la API no pone «Desconectado» a un paciente que está en la sala de espera.
7. Un paciente derivado aparece «En línea» en el buzón del **especialista**.
8. El anti-ráfaga sigue frenando el segundo correo dentro de los 15 min, y ahora lo hace
   también después de un reinicio.
9. Suite verde, cobertura ≥95 %, `ruff` limpio, migración aplicada con `python artisan migrate`.

## Decisiones cerradas

1. **Barrido con cron externo y no `asyncio.sleep` en el BackgroundTask.** Dormir 90 s dentro de
   una tarea de background retrasa el apagado ordenado de uvicorn hasta 90 s en cada despliegue,
   y el aviso se pierde igual si el proceso cae. La columna + barrido sobrevive al reinicio, no
   toca el apagado y se prueba sin relojes falsos. El precio es una línea más de cron, en el
   mismo cron que este repo ya necesita para los recordatorios.
2. **Columnas en `consultations` y no una tabla nueva.** El hilo ES la consulta
   (`.claude/rules/mensajeria.md`: «un hilo = una consulta»); una tabla de estado de avisos
   tendría exactamente una fila por consulta y la misma clave.
3. **Se difiere solo al paciente.** Diferir al médico exigiría presencia del médico, que la
   spec madre prohíbe exponer y que nadie registra.
4. **Nada de backfill.** Un barrido retroactivo sobre hilos con mensajes viejos sin leer
   mandaría una ráfaga de correos sobre casos que probablemente ya se resolvieron por otra vía.

## Open Questions

Ninguna.
