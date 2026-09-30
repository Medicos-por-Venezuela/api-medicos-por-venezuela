# TODO: Mensajería médico ↔ paciente

> Spec: [`spec.md`](./spec.md) · Plan: [`plan.md`](./plan.md)
> Estado: **no iniciado**. Bloqueos: preguntas P1–P7 de `.knowledge/mensajeria.md`.

## Fase 0 — Revisión y acuerdo (sin costo)

- [ ] T0.1 Revisar ambos repos y confirmar horas por fase al cliente por Workana
- [ ] T0.2 Videollamada con Ori y Adarvelys (30-sep): cerrar P1 (tarifa), P2 (Meta), P3 (buzón vs chat), P4, P5, P7
- [ ] T0.3 Registrar respuestas en `.knowledge/mensajeria.md` y desbloquear tareas

## Fase 1 — Buzón web (API)

- [ ] T1.1 Migración `mensajeria_hilos`: columnas de R1, índices, seed `messages.read`/`messages.write` (R12) — 2 h
- [ ] T1.2 `Message` ampliado, `schemas/message.py` (`MessageCreate`, `MessageResponse` con grant, `InboxThreadResponse`, `ReadReceiptResponse`) — 1 h
- [ ] T1.3 `services/messaging.py`: `thread_grant`, `send_message`, `list_messages`, `mark_read`, `inbox`, `unread_counts` + tests (grant médico/paciente/token/admin, ventana tras cierre, doble marcado concurrente) — 5 h · **bloquea P5**
- [ ] T1.4 `routers/messages.py`: R2, R3, R4, R5, R7 + Swagger + tests de router y rate limit — 3 h · **bloquea P4**
- [ ] T1.5 `notifications.py`: `message_received`, correos médico/paciente con debounce, test negativo «sin cuerpo en el correo» — 2 h · **bloquea P7**
- [ ] T1.6 SSE: evento `message` en `waiting_room` + `GET /inbox/stream` + tests — 2 h

### Checkpoint API Fase 1

- [ ] 0 failed, cobertura ≥95 %, ruff limpio, `migrate:status` sin pendientes
- [ ] README (endpoints) y `.knowledge/mensajeria.md` actualizados con lo que quedó
- [ ] PR `feat/mensajeria-buzon` → `dev`; verificación local adjunta

## Fase 1 — UI (repo `medicos-por-venezuela`, `tasks/mensajeria-medico-paciente/todo.md`)

- [ ] T1.7 Ejecutado y marcado en el repo frontend — 5 h

## Fase 2 — Puente WhatsApp (API)

- [ ] T2.1 `Settings` WhatsApp (R14) + `services/whatsapp.py` (`send_text`, `send_template`, reintento, no-op sin token) + tests con `httpx` mock — 4 h · **bloquea P2**
- [ ] T2.2 Consentimiento (R9): migración, `PatientCreate.whatsapp_consent`, `POST/DELETE /patients/{id}/whatsapp-consent` + tests — 2 h
- [ ] T2.3 `routers/whatsapp_webhook.py`: GET verify, POST firmado, 200 rápido, idempotencia por `wamid`, `statuses` monótonos + tests (firma inválida, duplicado concurrente) — 5 h
- [ ] T2.4 Enrutado entrante (D6), `whatsapp_unrouted_messages`, correo a operación + tests — 3 h
- [ ] T2.5 Ventana de 24 h, plantilla `medico_respondio`, respaldo por correo en `failed` + tests — 2 h

### Checkpoint API Fase 2

- [ ] 0 failed, cobertura ≥95 %, ruff limpio, `migrate:status` sin pendientes
- [ ] `.env.example` y `.env.production.example` con las variables de R14 documentadas
- [ ] PR `feat/mensajeria-whatsapp` → `dev`

## Fase 2 — UI e infraestructura

- [ ] T2.6 UI: estados de entrega, consentimiento en registro, privacidad, E2E — 2 h (repo frontend)
- [ ] T2.7 Meta: app, número, plantilla aprobada, webhook público tras Caddy (`/api/v1/webhooks/whatsapp`), prueba real con un paciente de prueba — 1 h · **se inicia el primer día de la fase**

### Checkpoint final

- [ ] Recorrido completo en producción con paciente de prueba (web → WhatsApp → web)
- [ ] Horas reales reportadas en Workana por fase

## Horas reales

| Fase | Estimadas | Reales | Nota |
|---|---|---|---|
| 0 | 1–2 (sin costo) | | |
| 1 API | 15 | | |
| 1 UI | 5 | | |
| 2 API | 16 | | |
| 2 UI + infra | 4 | | |
