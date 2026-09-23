# Cifrado de datos clínicos y acceso por necesidad de saber

Estado: aprobado (2026-09-23). Decisiones del equipo:

1. **Cola:** el médico habilitado cuya cola incluye un caso SIN asignar ve el motivo y los
   antecedentes/alergias (nivel SUMMARY) para decidir si lo toma. No ve notas.
2. **Interconsulta:** el invitado a una interconsulta en vivo y el especialista que TOMÓ una
   solicitud asíncrona son equipo tratante de ese caso (SUMMARY + NOTES). El inbox de
   solicitudes sin tomar es como la cola: SUMMARY.
3. **Correos e .ics:** sin texto clínico. Solo especialidad, código y enlace.
4. **Alcance:** API completa + ajustes mínimos del frontend (quitar al admin la edición de la
   nota del médico y pintar el marcador de confidencial).

## Modelo de acceso

| Quién | Nivel | Cómo se decide |
|---|---|---|
| Paciente dueño | SUMMARY (su motivo, sus antecedentes) | El servicio ya filtra `patients.user_id = caller` |
| Médico habilitado asignado | SUMMARY + NOTES | `assigned_doctor_id == principal.id` y ejerce (`DOCTOR_ROLES` + `is_staff`) |
| Invitado a interconsulta / especialista que tomó la solicitud | SUMMARY + NOTES | El servicio ya comprobó que es el invitado/el que tomó |
| Médico habilitado, caso sin asignar en su cola | SUMMARY | `queue_scope(is_admin=False).allows(specialty_id)` |
| admin / super_admin | ninguno | Ve estado, prioridad, asignación, `nota_admin`, métricas |
| Cualquier otro | ninguno | |

Un admin que además ejerce como médico recibe lo que le toca como médico, nunca por ser admin.

## Piezas ya escritas (no reescribir; usar)

- `src/core/clinical_crypto.py`: AES-256-GCM, formato `enc:v1:<kid>:<b64>`, AAD = "tabla.columna",
  llavero con rotación, `Sealed` (lo que devuelve el ORM; `str()` da el marcador), `reveal()`.
- `src/db/encrypted.py`: `EncryptedText("tabla.columna")`. Ya aplicado en los modelos:
  consultations.{chief_complaint, clinical_notes, internal_note}, consultation_events.note,
  interconsultations.note, interconsultation_requests.{chief_complaint, clinical_notes,
  closing_note}, patients.{description, allergies}, y las 6 tablas de `models/clinical.py`.
  **Al leer de la BD, estos atributos son `Sealed`, no `str`.** Tras asignar un `str` en la misma
  petición, el atributo sigue siendo ese `str` (expire_on_commit=False).
- `src/schemas/clinical.py`: `ClinicalSummary` / `ClinicalNote` (tipos para campos de SALIDA),
  `ClinicalAccessMixin` (añade `clinical_access: "full"|"summary"|"none"`), `clinical_context(grant)`,
  `summary_grant`, `treating_grant`. Sin contexto → null (fail-closed).
- `src/services/clinical_access.py`: `treating_doctor_grant`, `interconsultation_grant`,
  `patient_owner_grant`, `queue_grant` + `grant_for_queue_item`, `audit_clinical_read`,
  `audit_clinical_denied`, `READ_CLINICAL_DATA`.
- `src/core/observability.py::client_ip(request)` para la IP del audit.

## Reglas de implementación

- Esquemas de SALIDA: cada campo clínico pasa a `ClinicalSummary` (motivo `chief_complaint`,
  `patients.description`, `patients.allergies`, `interconsultation_requests.chief_complaint`) o
  `ClinicalNote` (internal_note, clinical_notes, notas de eventos, motivo de derivación,
  interconsultations.note, interconsultation_requests.clinical_notes/closing_note). Las
  respuestas que llevan campos clínicos heredan `ClinicalAccessMixin`.
  Esquemas de ENTRADA (`*Create`/`*Update`/requests) siguen con `str`.
- Router: `Schema.model_validate(obj, context=clinical_context(grant))`. Si `grant` no es None,
  `await audit_clinical_read(db, principal=..., ip=client_ip(request), resource="consultations",
  grants=[(obj.id, grant)])`. En listados, una llamada con todos los pares.
- Escritura de campos clínicos (`chief_complaint`, `clinical_notes`, `internal_note` por PATCH,
  eventos con nota): solo el médico tratante (asignado y habilitado). Un admin que manda uno de
  esos campos recibe 403 (`ForbiddenError`); sí puede cambiar `status`, `priority`,
  `assigned_doctor_id`, `nota_admin`, `admin_seguimiento`.
- Código que necesita el texto en el servidor (p. ej. copiar el motivo a la consulta hija):
  asignar el `Sealed` tal cual (el tipo lo re-cifra si cambia de columna). Si de verdad hace
  falta el texto: `reveal()` y nunca loguearlo.
- Nada de `ilike`/`order_by`/`==` sobre columnas cifradas.
- Tests: donde se compara un atributo ORM con un string, usar `reveal(obj.campo)`. Los tests
  que usan el `client` admin y esperaban texto clínico ahora deben esperar `null` +
  `clinical_access == "none"`; añadir el caso positivo con el médico asignado.

## Brechas previas que se cierran aquí

- `GET /consultations/{id}/chain` y `GET /{id}/events`: sin control de pertenencia. Pasan a:
  médico tratante del caso pedido → NOTES; admin → 200 con campos clínicos en null; otro → 403
  (y `audit_clinical_denied`).
- `GET /interconsultations/for-consultation/{id}`: igual (tratante o admin redactado).
- `GET /queue` y `POST /queue/{id}/take` devolvían `ConsultationResponse` con notas: ahora
  pasan por el contexto (SUMMARY en cola; al tomar, tratante).
- `/entered-call` y `/video-room` devolvían el esquema de staff a un paciente/anónimo: sin
  contexto → campos clínicos en null.
- Reportes Excel (super_admin): fuera `chief_complaint` y el filtro `ilike` por motivo en
  consultas; fuera `description` y `allergies` en pacientes.
- Correos de interconsulta y feed .ics: sin motivo.

## Auditoría

`audit_log.action = 'READ_CLINICAL_DATA'`, `actor_user_id`, `resource` (`consultations`,
`patients`, `interconsultation_requests`), `resource_id` (si es uno), `metadata = {outcome,
via, tiers, ids}`, `ip`, `correlation_id`, `created_at`. Un listado = una fila con los ids.

## Base de datos

- `db/migrations/20260923_134911_rls_tablas_clinicas_deny_all.sql`: deny-all en tablas clínicas,
  fuera las policies `USING (true)` de `treatment_plans`/`messages`, fuera TRUNCATE, índice GIN
  del audit.
- `db/post-backfill/…_clinical_ciphertext_checks…sql`: CHECK `enc:v1:%`. Se promueve a
  `db/migrations/` en un PR posterior, tras el backfill en prod.
- `scripts/encrypt_clinical_data.py`: backfill/rotación en Python (la clave no toca SQL).
