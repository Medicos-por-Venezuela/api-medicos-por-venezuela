# Spec: Mensajería médico ↔ paciente (buzón web + puente WhatsApp)

> Intención confirmada con el cliente por el chat de Workana (21 → 28 de septiembre de 2026;
> acuerdos y preguntas abiertas en `.knowledge/mensajeria.md`). Pendiente de videollamada con
> Ori y Adarvelys (30-sep) para cerrar las preguntas P1–P7.
> Fases posteriores: `tasks/mensajeria-medico-paciente/plan.md` y `todo.md`.
> Abarca DOS repos: `api-medicos-por-venezuela` (dominio, esta spec es la canónica) y
> `medicos-por-venezuela` (UI, `tasks/mensajeria-medico-paciente/` de aquel repo).
> Reglas duras del módulo: `.claude/rules/mensajeria.md`.

## Objective

Que el médico voluntario y el paciente puedan seguir conversando **después** de que el caso entró
en la plataforma, sin que ninguno vea el teléfono del otro. El médico escribe y lee desde un
**buzón en la web**; el paciente lee y responde desde la web (cuenta o enlace con token) y, en la
segunda fase, desde su **WhatsApp** a través del número oficial de la plataforma (API Cloud de
Meta). Todo mensaje queda guardado, cifrado, en el historial de la consulta.

### Actores

| Actor | Qué gana |
|---|---|
| **Médico tratante** | Un buzón donde ve qué pacientes le escribieron, responde y deja constancia, sin dar su número. |
| **Paciente** (con cuenta o anónimo) | Recibe la respuesta de su médico donde ya está (WhatsApp) o en la web, sin mantener una pestaña abierta. |
| **Administración** | Sabe que la conversación existe (conteos, estados, fechas) sin leerla. |
| **Operación** | Recibe aviso de mensajes de WhatsApp que no se pudieron enrutar a ningún caso. |

### Por qué ahora

Hoy la atención es solo por videoconsulta y la sala de espera. Si el paciente no entra a tiempo o
el médico necesita un seguimiento, el único canal es el teléfono personal del médico (botón
«Contactar por WhatsApp» del modal previo a la llamada), lo que expone su número y no deja rastro
en la consulta. El cliente lo describe como «la única cosa que necesito que resolvamos».

### Historias de usuario

Fase 1 — buzón web:

1. Como médico, en el detalle de una consulta mía veo el hilo de mensajes y escribo uno nuevo.
2. Como médico, tengo un buzón con todos mis hilos, ordenado por último mensaje, con no leídos.
3. Como médico, recibo un correo cuando un paciente me escribe (si no lo desactivé en preferencias)
   y, si tengo el panel abierto, el contador se actualiza sin recargar.
4. Como paciente con cuenta, en `/mi-caso` leo lo que me escribió el médico y respondo.
5. Como paciente sin cuenta, recibo un correo con un enlace (si dejé correo) y desde la sala de
   espera, con mi token, leo y respondo.
6. Como administrador, en el caso veo cuántos mensajes hay y cuándo fue el último, no su contenido.

Fase 2 — puente WhatsApp:

7. Como paciente, al registrarme acepto (o no) recibir mensajes de mi médico por WhatsApp.
8. Como paciente que aceptó, recibo en mi WhatsApp lo que escribe el médico y respondo ahí; mi
   respuesta aparece en el buzón del médico.
9. Como médico, veo si mi mensaje fue enviado, entregado, leído o falló en WhatsApp.
10. Como paciente que respondió hace más de 24 horas, recibo una plantilla «tu médico respondió,
    entra aquí» con enlace, y leo el mensaje completo en la web.
11. Como operación, recibo aviso de un mensaje de WhatsApp de un número que no corresponde a
    ningún caso abierto.

## Tech Stack

- **Backend** — Python 3.12, FastAPI async, SQLAlchemy 2.0 (asyncpg), Pydantic v2, PostgreSQL 17
  (Supabase), `uv`, Ruff, pytest + pytest-asyncio. Cifrado clínico `src/core/clinical_crypto.py`.
- **Correo** — Mailtrap vía `src/services/mail.py` (best-effort). **Producción real** para el cliente.
- **WhatsApp** — API Cloud de Meta (Graph API `/{phone_number_id}/messages`, webhooks firmados).
  Cliente HTTP con `httpx` async. Sin SDK adicional (evaluar `pip-audit` si se añade alguno).
- **Tiempo real** — SSE (patrón de `services/waiting_room.py`).
- **Frontend** — Next.js 16 (Pages Router), React 19, TypeScript, Playwright, pnpm. Todo dato por
  `lib/apiClient.ts`.

## Commands

### API (`api-medicos-por-venezuela`)

```
Deps:      uv sync --extra dev
Dev:       uv run uvicorn src.main:app --reload --workers 1
Test:      uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
Lint:      uv run ruff check . --fix && uv run ruff format .
Migración: python artisan make:migration <nombre>   /   python artisan migrate
Entorno:   npx supabase start   (Supabase local; ver README)
```

### Frontend (`medicos-por-venezuela`)

```
Dev:    pnpm dev
Build:  NEXT_DIST_DIR=.next-e2e pnpm build
Types:  pnpm exec tsc --noEmit
Lint:   pnpm lint
E2E:    pnpm test:e2e
```

## Project Structure

```
api-medicos-por-venezuela/
├── db/migrations/
│   ├── AAAAMMDD_HHMMSS_mensajeria_hilos.sql          → amplía messages, índices, permisos RBAC
│   └── AAAAMMDD_HHMMSS_mensajeria_whatsapp.sql       → consentimiento en patients, wa_message_id único, tabla de no enrutados
├── src/core/config.py                                → WHATSAPP_*, MESSAGING_* (ver R14)
├── src/models/clinical.py                            → Message ampliado
├── src/models/patient.py                             → whatsapp_consent_at, whatsapp_consent_source
├── src/models/messaging.py                           → WhatsappUnroutedMessage
├── src/schemas/message.py                            → MessageCreate, MessageResponse, InboxThreadResponse, ReadReceiptResponse
├── src/services/messaging.py                         → hilo, grants, envío, lectura, buzón, contadores, SSE del buzón
├── src/services/whatsapp.py                          → cliente Meta, firma, parseo, enrutado, estados
├── src/services/notifications.py                     → evento message_received + correos de aviso
├── src/services/waiting_room.py                      → evento `message` en el stream
├── src/routers/messages.py                           → /consultations/{id}/messages, /inbox, /inbox/stream
├── src/routers/whatsapp_webhook.py                   → /webhooks/whatsapp
├── src/routers/patients.py                           → consentimiento
└── tests/test_messaging*.py, tests/test_whatsapp*.py

medicos-por-venezuela/                                → ver tasks/mensajeria-medico-paciente/ de ese repo
```

## Requisitos

Cada requisito nombra quién puede, con qué permiso o pertenencia, y qué devuelve.

### R1 — Un hilo por consulta, en la tabla `messages` ampliada

Columnas finales de `messages`: `id`, `consultation_id` (FK CASCADE, hilo), `sender_role`
(`doctor | patient | system`), `sender_user_id` (FK `users`, nulo para paciente anónimo),
`direction` (`doctor_to_patient | patient_to_doctor`), `channel` (`web | whatsapp`), `body`
(cifrado, `EncryptedText`), `sent_at`, `delivered_at`, `read_at`, `delivery_status`
(`sent | delivered | read | failed`), `wa_message_id` (texto, único cuando no es nulo),
`error_code` (texto, nulo). Índices: `(consultation_id, sent_at, id)` y `(wa_message_id)` único parcial.

- CA1.1 La migración es idempotente y transaccional; conserva las filas existentes (hoy ninguna).
- CA1.2 RLS sigue deny-all; el CHECK de ciphertext sigue vigente.
- CA1.3 El hilo vigente de un paciente derivado es el de la consulta hija
  (`waiting_room.current_in_chain`); el histórico se lee hacia abajo por la cadena.

### R2 — El médico escribe

`POST /consultations/{id}/messages` con `{ "body": str }`, permiso `messages.write`.

- CA2.1 Solo el médico tratante (`assigned_doctor_id`) o quien lo fue en la cadena de esa
  consulta; otro médico recibe 404 (no 403, para no revelar existencia).
- CA2.2 La consulta debe estar en `in_progress`, `scheduled`, `referred_to_specialist` o cerrada
  hace menos de `MESSAGING_AFTER_CLOSE_HOURS` (72 por defecto, ver P5); si no, 409.
- CA2.3 `body` de 1 a 2000 caracteres, sin etiquetas HTML (validador Pydantic), `extra="forbid"`.
- CA2.4 Responde 201 con `MessageResponse` (cuerpo visible para el autor). Registra
  `audit_log` `message.sent` sin contenido.
- CA2.5 Dispara el aviso al paciente (R6) y, en Fase 2, el envío por WhatsApp (R10).

### R3 — Leer el hilo

`GET /consultations/{id}/messages?limit=&offset=` ordenado por `sent_at, id`.

- CA3.1 Grant: médico tratante actual o previo en la cadena (`messages.read` + pertenencia),
  paciente dueño (sesión con `owns_patient` o `X-Consultation-Token` válido de esa consulta).
- CA3.2 Sin grant, el cuerpo sale `null` (fail-closed) y la respuesta no falla; el admin con
  `consultations.read` recibe metadatos y cuerpos `null`.
- CA3.3 Toda lectura concedida se audita con `READ_CLINICAL_DATA` (vía `messages`, una entrada por
  página, no por mensaje).
- CA3.4 Incluye `unread_count` para el llamante (mensajes de la otra dirección sin `read_at`).

### R4 — El paciente escribe

Mismo endpoint que R2, sin permiso de staff: pertenencia (sesión) o token de consulta.

- CA4.1 `sender_role = patient`, `sender_user_id` nulo si es anónimo.
- CA4.2 Rate limit `PUBLIC_WRITE_RATE_LIMIT` por IP y, además, máximo 30 mensajes por hilo y hora.
- CA4.3 Ventana: consulta abierta o cerrada hace menos de `MESSAGING_AFTER_CLOSE_HOURS`; si no, 409
  con mensaje «Esta consulta ya no admite mensajes».
- CA4.4 Dispara el aviso al médico (R6).

### R5 — Marcar leído

`POST /consultations/{id}/messages/read` → `{ "marked": n }`.

- CA5.1 Marca `read_at = now()` solo en mensajes de la dirección contraria con `read_at IS NULL`
  (escritura condicional, `rowcount`).
- CA5.2 Mismo grant que R3. Idempotente.

### R6 — Avisos por correo

- CA6.1 Al médico: evento `message_received` en `NOTIFICATION_EVENTS` con canal `email`; respeta
  `should_send`. Asunto «Tu paciente te escribió», enlace a `/panel-medico/consulta/{id}`. Sin cuerpo.
- CA6.2 Al paciente: si `patients.email` existe, correo «Tu médico te respondió» con enlace
  tokenizado a `/sala-espera` (token de consulta, TTL 24 h) o a `/mi-caso` si tiene cuenta. Sin cuerpo.
- CA6.3 Anti-ráfaga: no se envía un segundo correo al mismo destinatario por el mismo hilo si el
  anterior salió hace menos de `MESSAGING_MAIL_DEBOUNCE_MINUTES` (15) y sigue sin leer.
- CA6.4 Todo por `mail.best_effort` + `BackgroundTasks`; un fallo nunca rompe el envío del mensaje.

### R7 — Buzón del médico

`GET /inbox?limit=&offset=&only_unread=` permiso `messages.read`.

- CA7.1 Un elemento por consulta con mensajes donde el llamante es o fue tratante: `consultation_id`,
  `code`, especialidad, nombre visible del paciente (el mismo que ya ve en el panel), `status`,
  `last_message_at`, `last_direction`, `unread_count`. Sin cuerpos.
- CA7.2 Orden `last_message_at desc, consultation_id`. Paginado (máx. 100).

### R8 — Tiempo real

- CA8.1 `GET /consultations/{id}/waiting-room/stream` emite, además de la fase, el evento
  `message` `{ message_id, direction, sent_at, unread_count }` cuando hay uno nuevo hacia el
  paciente. Sin cuerpo.
- CA8.2 `GET /inbox/stream` (SSE, permiso `messages.read`) emite `inbox`
  `{ unread_total, updated: [consultation_id] }` con el patrón poll/heartbeat/max de `Settings`.
- CA8.3 El frontend refetch al recibir un evento; ningún cuerpo viaja por SSE.

### R9 — Consentimiento de WhatsApp (Fase 2)

- CA9.1 `patients.whatsapp_consent_at` (timestamptz, nulo) y `whatsapp_consent_source`
  (`registration | web | whatsapp`). `PatientCreate` acepta `whatsapp_consent: bool` (por defecto
  falso). `POST /patients/{id}/whatsapp-consent` y `DELETE` para conceder/revocar desde la web
  (pertenencia o token).
- CA9.2 Sin consentimiento no hay ningún envío saliente; revocar detiene los envíos de inmediato.
- CA9.3 Un mensaje entrante del paciente por WhatsApp vale como consentimiento
  (`source = whatsapp`) para responder dentro de la ventana de 24 h.
- CA9.4 `pages/legal/privacidad.tsx` describe el canal y su fecha se actualiza (repo frontend).

### R10 — Envío saliente por WhatsApp (Fase 2)

- CA10.1 Al crearse un mensaje `doctor_to_patient`, si hay consentimiento y `patients.phone`
  normalizado a E.164, `services/whatsapp.send` decide: dentro de las 24 h del último mensaje
  entrante → texto libre con el cuerpo; fuera → plantilla `medico_respondio` con el enlace
  tokenizado; guarda `wa_message_id`, `channel = whatsapp` en una fila **hija** de tipo `system`
  o actualiza `delivery_status` del mensaje original (decisión D5 del plan).
- CA10.2 Un fallo de Meta deja `delivery_status = failed` con `error_code` y dispara el correo de
  R6 como respaldo; un 5xx se reintenta una vez.
- CA10.3 Sin `WHATSAPP_ACCESS_TOKEN` el conector es no-op con warning (igual que Mailtrap).
- CA10.4 Nunca se loguea número, nombre ni cuerpo; sí `wamid`, tipo y código de error.

### R11 — Webhook entrante (Fase 2)

`GET /webhooks/whatsapp` (verificación con `WHATSAPP_VERIFY_TOKEN`) y `POST /webhooks/whatsapp`.

- CA11.1 Firma `X-Hub-Signature-256` verificada con `WHATSAPP_APP_SECRET`; firma inválida → 403 y
  log WARNING sin contenido.
- CA11.2 Responde 200 en menos de 2 s; el procesamiento va a `BackgroundTasks`.
- CA11.3 Idempotente por `wamid`: un evento repetido no crea dos filas ni retrocede un estado.
- CA11.4 `messages` entrantes: `teléfono → paciente (phone E.164) → consulta vigente`
  (`current_in_chain` de la consulta abierta más reciente de ese paciente); crea `Message`
  `patient_to_doctor`, `channel = whatsapp`; dispara el aviso al médico (R6) y el SSE (R8).
- CA11.5 Sin paciente o sin consulta que admita mensajes → fila en `whatsapp_unrouted_messages`
  (número cifrado, `wamid`, fecha; sin cuerpo) y correo a `MAIL_INTERNAL_RECIPIENTS`.
- CA11.6 `statuses` (`sent`, `delivered`, `read`, `failed`) actualizan `delivery_status` de forma
  monótona (`sent < delivered < read`; `failed` solo desde `sent`).

### R12 — Permisos RBAC

- CA12.1 Migración siembra `messages.read` y `messages.write` para `doctor`, `admin`,
  `super_admin`; el admin, aun con `messages.read`, no obtiene grant de cuerpo (R3).
- CA12.2 El paciente nunca pasa por permisos de staff: solo pertenencia o token.

### R13 — Seguridad y PII

Aplican `security.md` y `mensajeria.md`: cuerpos cifrados y auditados; nada del cuerpo en correo,
SSE, logs, Excel ni `.ics`; teléfonos nunca en respuestas de mensajería; webhook firmado e
idempotente; secretos solo por `Settings`.

### R14 — Configuración

`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TEMPLATE_DOCTOR_REPLIED` (`medico_respondio`),
`WHATSAPP_TEMPLATE_LANGUAGE` (`es`), `MESSAGING_AFTER_CLOSE_HOURS` (72),
`MESSAGING_MAIL_DEBOUNCE_MINUTES` (15), `MESSAGING_PATIENT_HOURLY_LIMIT` (30). Todas en
`.env.example` con comentario; en producción el arranque falla si `WHATSAPP_ACCESS_TOKEN` está y
falta `WHATSAPP_APP_SECRET`.

## Requisitos no funcionales

- Cobertura ≥95 % en los módulos nuevos; tests de concurrencia para R5 (doble marcado) y R11
  (webhook duplicado en paralelo).
- SSE: latencia de aviso ≤ `WAITING_ROOM_POLL_SECONDS`; sin cuerpos.
- Webhook: 200 en < 2 s con Meta; procesamiento en background.
- Sin dependencias nuevas salvo `httpx` (ya presente por tests); si se añade alguna, `pip-audit`.

## Fuera de alcance (esta iteración)

- Chat en tiempo real con «escribiendo…», adjuntos, imágenes o audios.
- Push real (Web Push / FCM): `lib/firebase.ts` del frontend está sin conectar y no forma parte
  de este encargo; el canal `push` de `message_received` se añade cuando exista.
- Cita presencial como tipo de agenda (P6).
- Varios números de WhatsApp o mensajería a médicos por WhatsApp.
- Mensajes iniciados por el médico a pacientes sin consulta (marketing).

## Preguntas abiertas

Ver `.knowledge/mensajeria.md` § Preguntas abiertas (P1 tarifa, P2 Meta Business, P3 buzón vs
chat, P4 paciente responde por web, P5 ventana tras cierre, P6 cita presencial, P7 correo del
paciente anónimo). Las tareas que dependen de cada una lo indican en `todo.md` y **se detienen**
hasta tener respuesta del cliente por Workana.
