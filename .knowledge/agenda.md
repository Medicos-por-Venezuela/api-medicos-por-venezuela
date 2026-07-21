# Módulo Agenda

Citas agendadas para no dejar consultas abiertas: una cita = **fila en `consultations`** con
`scheduled_at` + `parent_consultation_id` (auto-FK, cadena padre→hijas) + status **`scheduled`**.
Firma del médico (`close_signature`, dataURL PNG) al cerrar/agendar. Ver también
[interconsultas.md](interconsultas.md) para la distinción entre los tres flujos.

## Los dos flujos de agenda (≠ Interconsulta)

- **Agendar seguimiento** — MISMO médico. Cierra la actual (`closed`, firmada) y crea una hija
  agendada (mismo `assigned_doctor_id`). `POST /consultations/{id}/schedule-follow-up`.
- **Agendar con especialista** (referencia) — OTRO médico. La actual queda `referred_to_specialist`
  (derivada, ya no la atiende el médico actual); crea la hija agendada asignada al especialista con
  el **motivo firmado** (motivo → `internal_note` de la hija). El referido la ve en su agenda y ve
  las notas previas por el chain. `POST /consultations/{id}/refer`.

Lo ven: el médico en `GET /consultations/agenda` (sección "Mi agenda" de `/panel-medico`) y el
paciente en su portal (`/mi-caso`, por su propio scoping). Cadena/historial:
`GET /consultations/{id}/chain`.

## Notificaciones (Fase 3)

`src/services/notifications.py` sobre `services/mail.py` (Mailtrap, best-effort: un fallo de correo
NUNCA rompe el agendado). Solo se escribe al paciente si `patients.email` no es nulo.

- **Al agendar**: los endpoints `schedule-follow-up` y `refer` encolan el correo "cita agendada"
  con `BackgroundTasks` de FastAPI (no bloquea la respuesta).
- **~30 min antes**: `POST /consultations/agenda/send-due-reminders?window_minutes=30`
  (permiso `queue.manage`). Busca las citas `scheduled` con `scheduled_at` en [ahora, ahora+ventana]
  y `reminder_sent_at` nulo, envía y setea `reminder_sent_at` (idempotente). **1 solo intento**: si
  el correo falla no reintenta (el correo confiable es el de "al agendar").
- **Notificaciones nativas del navegador** (frontend, `lib/nativeNotifications.ts`): aviso al
  agendar y recordatorio local ~30 min antes. Techo: sin service worker + Web Push, solo disparan
  con la pestaña abierta → son un complemento; el email es el canal confiable.

## Sincronización con calendarios (iCalendar)

El médico ve su agenda en `/panel-medico` (sección "Mi agenda") y el paciente en `/mi-caso`. Ambos
pueden **sincronizarla** con Google Calendar, Apple (iPhone/Mac), Outlook, etc. usando **iCalendar
(RFC 5545)** — un solo formato para todos los proveedores, sin integrar cada uno aparte. Dos vías:

- **Suscripción (feed, se sincroniza solo)**: `GET /agenda/{token}.ics` devuelve un VCALENDAR con
  las citas del dueño del token (médico → sus asignadas; paciente → las suyas). El calendario del
  usuario sondea esa URL y se mantiene al día. `GET /agenda/calendar-url` (autenticado) da la URL
  `webcal://…/{token}.ics` (genera el token la 1ª vez); `POST /agenda/calendar-url/rotate` lo
  regenera. Servicio `src/services/calendar.py`, router `src/routers/agenda.py`.
- **Descarga `.ics` por cita** (import puntual): botón "Agregar a calendario" generado en el
  navegador (`lib/calendar.ts::downloadIcs`), sin backend.

**Auth del feed**: el `.ics` NO usa el JWT (Google/Apple sondean sin él) — autentica por
`users.calendar_token` (uuid secreto, no adivinable, de solo lectura, **regenerable**). Es el patrón
de "dirección secreta de calendario" de Google. Como el feed lleva PII (nombres), la URL es un
secreto: la UI avisa de no compartirla. **Deploy**: el endpoint `/agenda/{token}.ics` debe quedar
**accesible desde internet** (los servidores de Google/Apple lo consultan), no solo desde el
frontend; y `request.base_url` sale bien detrás de proxy con `uvicorn --proxy-headers`.

## Preferencias de notificación (para que no sea invasivo)

Cada usuario controla qué notificaciones recibe y por qué canal, desde **Ajustes** en su perfil
(`/panel-medico/perfil`, layout con sidebar). Modelo: `users.notification_prefs` JSONB
`{ "<evento>": {"push": bool, "email": bool} }`. **Opt-out**: preferencia ausente = habilitada
(`{}` = todo activado). Catálogo (fuente de verdad en `notifications.NOTIFICATION_EVENTS`):

| evento | canales | hoy |
|---|---|---|
| `appointment_reminder` | push, email | recordatorio ~30 min antes (push nativo + correo al médico) |
| `appointment_confirm` | push | aviso al agendar/referir (tu propia acción) |
| `interconsultation_assigned` | email | **correo** al invitado al asignar interconsulta |
| `referral_received` | email | **correo** al especialista al referirle un paciente |

(interconsulta/referencia son email-only por ahora: el push nativo de esos eventos requeriría
realtime + pestaña abierta del destinatario — pendiente.)

- **Endpoints**: `GET`/`PATCH /me/notification-preferences` (autenticado). El PATCH sanea contra el
  catálogo (`sanitize_prefs`). El GET devuelve además el catálogo para que la UI no lo duplique.
- **Respeto — email** (backend): `should_send(prefs, event, "email")` antes de encolar; los routers
  de interconsulta/refer usan `doctor_event_email_args` (devuelve None si desactivado o sin email);
  `send_due_reminders` chequea la pref del médico.
- **Respeto — push** (frontend): `lib/notificationPrefs.isPushEnabled(prefs, event)` gatea `notify`
  (consulta detail) y `scheduleLocalReminders` (panel-medico) antes de disparar.
- **Auditoría previa**: antes de esto el médico recibía 3 avisos nativos y 0 correos; los correos de
  cita (agendada/recordatorio) van al paciente, no al médico.

## Cron externo para los recordatorios

El backend NO tiene scheduler; un cron externo golpea el endpoint gateado cada 1–5 min. El token es
de un usuario con `queue.manage` (mismo patrón que `POST /queue/release-stale`).

Ejemplo (crontab en la EC2, cada 5 min):

```cron
*/5 * * * * curl -fsS -X POST \
  -H "Authorization: Bearer $MPV_CRON_TOKEN" \
  "https://api.medicosporvenezuela.org/api/v1/consultations/agenda/send-due-reminders" \
  >/dev/null 2>&1
```

Alternativa: GitHub Actions `schedule` (cron) con el token en secrets. La ventana por defecto (30
min) tolera un cron de hasta ~5 min sin perder recordatorios. Sin `MAILTRAP_API_TOKEN` el envío es
no-op (solo loguea), útil en local/staging.
