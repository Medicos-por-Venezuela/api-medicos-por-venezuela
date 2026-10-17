# Plan: Avisos del chat y presencia del paciente

> Fase 2 de 3. Spec: `tasks/avisos-chat-presencia/spec.md`. Tareas: `todo.md`.
> Rama propuesta: `feat/avisos-chat-presencia` sobre `dev`.

## Estrategia: cuatro rebanadas, ordenadas por riesgo creciente

Cada rebanada deja una feature del chat **funcionando y probada**. El orden no es por
dificultad: es porque las dos primeras no tocan la forma de decidir si se envía, y las dos
últimas sí. Si el trabajo se corta a la mitad, lo entregado sirve y nada queda a medio migrar.

```
Rebanada 1: R1 — nombre del médico y primer contacto   ── solo composición ── entregable sola
        │                                                  (cero riesgo de entrega)
        ▼
Rebanada 2: R2 — presencia persistente y por cadena    ── no cambia ningún envío
        │                                                  (habilita la 3)
        ▼
Rebanada 3: R4 — anti-ráfaga en BD, una sola regla     ── retira el dict en memoria
        │                                                  (la 4 se apoya en sus columnas)
        ▼
Rebanada 4: R3 — diferir al paciente en línea          ── requiere 2 y 3
```

**R4 va antes que R3 a propósito**, aunque el valor visible de R3 sea mayor. R3 necesita
escribir `patient_notice_pending_at` en la misma función que hoy llama a `record_mail_debounce`:
si se hace primero, esa función acaba escribiendo a la vez en el `dict` de memoria y en una
columna, y el paso siguiente tiene que desmontar las dos. Haciendo R4 antes, R3 añade un campo
a una estructura que ya está donde tiene que estar.

## Grafo de dependencias

```
[1] migración (3 columnas) ──┬──> [5] debounce en BD (R4) ──> [6] retirar _LAST_MAIL_NOTIFICATIONS
                             └──> [8] diferido (R3) ──> [9] barrido ──> [10] endpoint del barrido
[2] modelo Consultation ─────┘

[3] _build del correo (R1) ──> [4] wiring de doctor_name + primer contacto

[7a] throttle de presencia (R2) ──┬──> [7b] las 4 señales de messaging
[7c] touch_presence en sse_events ┘                    └──> [8]
```

`[3]`/`[4]` son independientes de todo lo demás y pueden ir en paralelo con `[1]`/`[2]`.
`[5]` y `[8]` tocan ambos `message_received_mail_args`: **secuenciales**, no en paralelo.

## Rebanada 1 — Nombre del médico y primer contacto (R1)

**Entrega:** el paciente recibe un correo que identifica a su médico y que no le dice
«respondió» cuando nadie preguntó nada.

1. **`patient_message_received_email`** gana dos parámetros: `doctor_name: str | None` y
   `is_first_contact: bool`. Se mantiene pura (sin sesión, sin IO) — es la capa que el spec
   manda probar a fondo.
2. **Asunto y primera línea** se derivan de los dos. Sin nombre cae a «Tu médico», igual que
   `video_ready_email` cae a «Tu médico» con `quien = doctor_name or "Tu médico"`. No se
   inventa un tratamiento («Dr.»): `Profile.full_name` ya viene como el médico lo escribió, y
   anteponerle un título produce «Dr. Dra. Ana Pérez» en cuanto alguien lo incluye.
3. **`message_received_mail_args`** resuelve los dos valores con la sesión viva: `_doctor_name`
   (que ya existe) y un `COUNT` de mensajes `patient_to_doctor` en el hilo.

**Por qué un COUNT y no un flag en la consulta:** el flag habría que mantenerlo en los dos
sentidos y quedaría desincronizado con la cadena de derivación. El COUNT es una consulta sobre
`(consultation_id, direction)`, el mismo acceso que el debounce ya hace dos líneas antes.

## Rebanada 2 — Presencia persistente y por cadena (R2)

**Entrega:** el buzón del médico deja de mentir tras un reinicio y con pacientes derivados.

1. **`touch_patient_presence(session, consultation_id)`** sustituye a `record_patient_presence`
   en las cuatro señales de `messaging.py`: escribe el `dict` siempre y la columna solo si
   pasaron `MESSAGING_PRESENCE_PERSIST_SECONDS` desde `patient_last_seen_at`. El throttle se
   decide con el valor que la fila ya tiene cargada: cero consultas extra.
2. **`sse_events` gana `touch_presence: Callable[[], Awaitable[None]] | None`.** Se inyecta
   igual que `fetch`: la función sigue sin saber qué es una sesión. El router la cablea con
   `session_factory`, abriendo y cerrando una sesión corta como ya hace `fetch`.
3. **El id correcto.** El router resuelve el caso vigente con `current_in_chain` —que ya llama
   antes de abrir el stream, para dar 404— y registra la presencia contra **ese** id. Hoy usa
   `requested_id` y por eso el especialista ve «Desconectado» a un paciente derivado.

**Riesgo controlado:** el throttle es lo único que separa esto de un UPDATE cada 4 s por
paciente en espera sobre una tabla con CDC. Su test va **antes** del wiring del SSE.

## Rebanada 3 — Anti-ráfaga en base de datos (R4)

**Entrega:** una sola regla de anti-ráfaga, que sobrevive al reinicio y a una segunda réplica.

1. **Migración** `add column if not exists` × 3, transaccional, sin backfill.
2. **`should_debounce_mail`** se reescribe con la regla única del CA4.2 y deja de leer memoria.
   Pasa a recibir el `Consultation` ya cargado (lo tiene el caller) en vez de volver a pedirlo.
3. **Se retiran** `_LAST_MAIL_NOTIFICATIONS`, `record_mail_debounce` y `clear_mail_debounce`.
   `messaging.mark_as_read` pasa a limpiar la columna del lector dentro de **su propia
   transacción**, que es una mejora gratuita: hoy el `clear` en memoria ocurre después del
   `commit` y un fallo entre medias dejaba el freno puesto sobre una lectura ya persistida.

**Equivalencia que hay que preservar:** hoy el primer mensaje de un hilo siempre manda correo
(`unread == 1`, no `>= 2`). La regla nueva lo mantiene por otra vía: `patient_notice_sent_at`
está en `null`, así que no hay aviso previo del que protegerse. El test de los cuatro casos
(envía / frena / expira / tras leer) es el que lo fija.

## Rebanada 4 — Diferir el aviso al paciente en línea (R3)

**Entrega:** un paciente que está leyendo el chat deja de recibir correos redundantes, sin que
se pierda el aviso del que se fue.

1. **En `message_received_mail_args`**, rama `doctor_to_patient`: si `is_patient_online`, anota
   `patient_notice_pending_at` y devuelve `None` con un log `MAIL:deferred`.
2. **`mark_as_read`** limpia también `patient_notice_pending_at` cuando lee el paciente.
3. **`send_pending_message_notices(session)`**: una consulta por las consultas con pending
   vencido y mensajes sin leer, un intento por cabeza, limpia el pending pase lo que pase.
   Copia literal de la disciplina de `send_due_reminders`, incluido el «un solo intento».
4. **Endpoint** `POST /consultations/messages/send-pending-notices` con `queue.manage`, junto al
   de recordatorios y documentado para el mismo cron.

**El orden importa dentro de la tarea 3:** el pending se limpia *antes* de intentar el envío,
como `reminder_sent_at`. Limpiarlo después deja una ventana en la que dos pasadas del cron
solapadas mandan dos correos — justo el defecto que este trabajo viene a quitar.

## Qué NO entra

- **Redis para presencia en vivo.** Sigue siendo v2. Nada aquí lo necesita: anti-ráfaga y
  diferido toleran la granularidad de una fila, y la presencia en memoria se conserva como vía
  rápida con la columna detrás.
- **WhatsApp.** El canal no existe todavía (R9–R11 del spec madre, Fase 2). Cuando exista, el
  diferido de R3 es exactamente el punto donde engancha.
- **Escalada por mensaje sin leer tras N horas.** Es otra feature, con otra conversación de
  producto detrás (¿a quién se escala? ¿a operación?). No se cuela aquí.
- **Frontend.** Ninguna respuesta cambia de forma salvo el `ReminderRunResponse` gemelo del
  barrido, que no consume nadie más que el cron.
