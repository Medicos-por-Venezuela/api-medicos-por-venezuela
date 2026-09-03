# Tareas: Correos de alta (pacientes y médicos)

> Fase 3 de 4. Spec: `spec.md` · Plan: `plan.md`.
> Orden por **dependencia**, no por importancia. Ninguna tarea toca más de 5 ficheros.
> Al terminar cada rebanada: `uv run pytest --cov=src` verde y `ruff check`/`format` limpios.

## Rebanada 1 — Aviso de paciente nuevo (correo A)

- [x] **T1. Destinatarios internos configurables**
  - Acceptance: `settings.internal_mail_recipients` devuelve `list[str]` desde una variable
    separada por comas; **vacía por defecto**; ignora espacios y entradas en blanco.
  - Verify: `uv run pytest tests/test_config.py -v`
  - Files: `src/core/config.py`, `.env.example`, `tests/test_config.py`

- [x] **T2. Módulo de correo + composición del aviso de paciente**
  - Acceptance: `_build_new_patient(...)` devuelve asunto, texto y HTML con nombre, WhatsApp,
    zona, especialidad, código y enlace a `/admin/pacientes`. El asunto identifica el evento.
  - Verify: `uv run pytest tests/test_registration_mail.py -k paciente -v`
  - Files: `src/services/registration_mail.py`, `tests/test_registration_mail.py`

- [x] **T3. Test de frontera de PII (aserción negativa)**
  - Acceptance: dado un paciente **con** cédula, alergias y descripción, el texto y el HTML del
    correo A **no** contienen ninguno de los tres. El test falla si alguien añade el campo.
  - Verify: `uv run pytest tests/test_registration_mail.py -k pii -v`
  - Files: `tests/test_registration_mail.py`
  - Nota: va **antes** del wiring a propósito. Escrita después, esta prueba se convierte en una
    que confirma lo que el código ya hace; escrita ahora, es la que defiende la decisión.

- [x] **T4. `new_patient_mail_args` con sus dos guardas**
  - Acceptance: devuelve `None` si no hay destinatarios, si el paciente es de consultorio
    (`created_by_doctor_id`) o si la consulta está agendada (`scheduled_at`). Si no, devuelve
    valores planos ya resueltos (nada de objetos ORM).
  - Verify: `uv run pytest tests/test_registration_mail.py -k args -v`
  - Files: `src/services/registration_mail.py`, `tests/test_registration_mail.py`

- [x] **T5. Wiring en `POST /consultations`**
  - Acceptance: un alta pública encola el correo; una de consultorio y una agendada no. El
    endpoint sigue devolviendo `201` con el `access_token` de siempre.
  - Verify: `uv run pytest tests/test_consultations.py tests/test_registration_mail.py -v`
  - Files: `src/routers/consultations.py`, `tests/test_registration_mail.py`

- [x] **T6. Resiliencia: el correo no puede tumbar el alta**
  - Acceptance: con `mail.send_mail` lanzando, `POST /consultations` sigue devolviendo `201` y
    la consulta queda creada.
  - Verify: `uv run pytest tests/test_registration_mail.py -k resiliencia -v`
  - Files: `tests/test_registration_mail.py`

> **Checkpoint 1** — Correo A entregable solo. Prueba manual contra el sandbox de Mailtrap
> comprobando que el enlace abre el caso correcto.

## Rebanada 2 — Registro de médico (correos B, C, D)

- [x] **T7. `error_kind` tipado en los verificadores oficiales**
  - Acceptance: `SacsVerificationResponse` y `PsicologoVerificationResponse` ganan
    `error_kind: str | None`, fijado en cada `_fallo` (formato, HTTP, conexión, parseo, no
    registrada). El campo `error` en prosa se conserva tal cual. **Nadie hace string-matching**
    sobre los mensajes en español.
  - Verify: `uv run pytest tests/test_sacs.py tests/test_psicologo.py -v`
  - Files: `src/schemas/sacs.py`, `src/schemas/psicologo.py`, `src/services/sacs.py`,
    `src/services/psicologo.py`, `tests/test_sacs.py`

- [x] **T8. `CredentialCheck.reason` en el dominio**
  - Acceptance: `_verify_credential` devuelve uno de los cinco motivos del spec en vez del
    `_UNVERIFIED` único. Cada camino de rechazo (sin tipo, tipo inexistente, tipo no
    verificable, no encontrado, respuesta incompleta, servicio caído) tiene su test.
  - Verify: `uv run pytest tests/test_doctors.py tests/test_doctor_credential_gate.py -v`
  - Files: `src/services/doctors.py`, `tests/test_doctors.py`
  - Nota: el gate de credencial **no cambia de comportamiento**. `verified` sigue siendo
    fail-closed exactamente igual; lo único nuevo es que ahora se sabe por qué.

- [x] **T9. Correos B, C y D**
  - Acceptance: `DOCTOR_REJECTION_REASONS` mapea motivo → frase en español. D nombra los tres
    documentos (título, licencia SACS, carta de artículo 8), las dos direcciones de respuesta y
    **la cédula registrada**. B lleva cédula y motivo; C indica qué registro validó.
  - Verify: `uv run pytest tests/test_registration_mail.py -k "medico or motivo" -v`
  - Files: `src/services/registration_mail.py`, `tests/test_registration_mail.py`

- [x] **T10. Test de cobertura de motivos**
  - Acceptance: el test recorre las claves de `DOCTOR_REJECTION_REASONS` y exige que cada una
    produzca un texto **distinto**. Añadir un motivo sin su frase rompe la suite.
  - Verify: `uv run pytest tests/test_registration_mail.py -k motivos -v`
  - Files: `tests/test_registration_mail.py`

- [x] **T11. Wiring en `POST /doctors`**
  - Acceptance: credencial no válida encola B **y** D; válida encola C **y** E. El registro
    sigue devolviendo `201` en ambos casos, y también si el correo falla.
  - Verify: `uv run pytest tests/test_doctors.py tests/test_registration_mail.py -v`
  - Files: `src/routers/doctors.py`, `tests/test_registration_mail.py`

> **Checkpoint 2** — Los cinco motivos producen cinco textos distintos, verificados en el
> sandbox de Mailtrap.

## Rebanada 3 — Aprobación del admin (correo E)

- [x] **T12. `approve_doctor` informa si cambió el estado**
  - Acceptance: devuelve `ApprovalResult(doctor, newly_approved)`. Aprobar una ficha ya
    aprobada da `newly_approved=False` sin alterar nada más (sigue siendo idempotente y sigue
    dejando su entrada en `audit_log`, como hoy).
  - Verify: `uv run pytest tests/test_doctor_approval.py -v`
  - Files: `src/services/doctors.py`, `src/routers/doctors.py`, `tests/test_doctor_approval.py`

- [x] **T13. Correo E y resolución de destinatario**
  - Acceptance: usa `doctors.email`; si falta, `users.email` vía `user_id`; si tampoco hay, no
    envía y deja un warning **sin PII**. El cuerpo enlaza al panel médico.
  - Verify: `uv run pytest tests/test_registration_mail.py -k aprobado -v`
  - Files: `src/services/registration_mail.py`, `tests/test_registration_mail.py`

- [x] **T14. Wiring en `POST /doctors/{id}/approve` con idempotencia**
  - Acceptance: la primera aprobación encola **un** correo; la segunda, **ninguno**. Aprobar a
    un médico sin email en ninguna tabla responde `200` y no envía.
  - Verify: `uv run pytest tests/test_doctor_approval.py tests/test_registration_mail.py -v`
  - Files: `src/routers/doctors.py`, `tests/test_registration_mail.py`

> **Checkpoint 3** — Definition of Done del repo.

## Cierre

- [x] **T15. Suite completa y calidad**
  - Acceptance: suite verde, cobertura ≥95 %, `ruff check`/`format` limpios.
  - Verify: `uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto`

- [x] **T16. Despliegue documentado**
  - Acceptance: `MAIL_INTERNAL_RECIPIENTS` documentado en `.env.example` y en el README, con la
    advertencia de que **sin él no sale ningún aviso interno** (es el precio de R1).
  - Verify: revisión manual del diff.
  - Files: `.env.example`, `README.md`

## Fuera de alcance (recordatorio)

Campaña a los ~2 900 médicos del backlog · envíos retroactivos · pacientes de consultorio ·
plantillas HTML con diseño · frontend.

## Cierre de la implementación (2026-09-03)

Las 16 tareas están hechas. Dos cosas que aparecieron al implementar y no estaban en el plan:

- **Bug preexistente en `services/mail.py`.** Su docstring promete "nunca lanza", pero armaba
  el `mt.Mail(...)` **fuera** del `try`, y `mt.Address` valida el formato y lanza. Una coma
  mal puesta en `MAIL_INTERNAL_RECIPIENTS` habría reventado hacia el caller, rompiendo justo
  el flujo que ese módulo promete no romper. Arreglado moviendo el armado dentro del `try`;
  beneficia a todos sus usuarios (citas, recordatorios, interconsultas), no solo a esto.
- **`_best_effort` en el borde del BackgroundTask.** Lo encolado corre DESPUÉS de responder;
  si la composición del cuerpo reventara, se llevaría por delante el alta que el correo solo
  venía a anunciar. Se traga y loguea (sin PII). Es lo que hace verdad la promesa del spec.

**Verificación manual (2026-09-03).** Contra Supabase local y un uvicorn real (no TestClient),
con el envío deshabilitado a propósito — el `.env` de esta máquina tiene un token de Mailtrap
REAL y sin `MAILTRAP_INBOX_ID`, que es exactamente el riesgo R1:

| Acción | Resultado |
|---|---|
| `POST /consultations` (cola pública) | `201` + 1 correo `alta-paciente` |
| `POST /doctors` (SACS no valida) | `201` + `alta-medico` **y** `registro-medico`, mismo correlation_id |
| `POST /doctors/{id}/approve` sin licencia | `422` (guard existente, correcto) |
| `approve` x2 con ficha completa | `200`, `200` y **un solo** correo |
