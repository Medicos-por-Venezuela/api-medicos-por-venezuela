# Interconsultas

Segunda opinión **en tiempo real** entre médicos durante una consulta activa. No confundir con los
dos flujos de agenda (ver §Distinción).

## Distinción clave (¡importante!) — TRES flujos distintos

| | **Interconsulta** | **Agendar seguimiento** | **Agendar con Especialista** |
|---|---|---|---|
| Médico | Invita a otro (en vivo) | El **mismo** médico | **Otro** médico (referencia) |
| Consulta actual | **Sigue abierta** | Se **cierra** (`closed`) | Queda **`referred_to_specialist`** (derivada, ya no la atiende el actual) |
| Cuándo | Ahora, en vivo | Agenda hija para otro día | Agenda hija para otro día |
| Firma / motivo | — | Firma al cerrar | **Motivo firmado** |
| Qué ve el otro | Datos **limitados** (motivo/notas/edad) | — | **Notas previas** (chain): transfiere el cuidado |
| Endpoint | `POST /interconsultations` | `POST /consultations/{id}/schedule-follow-up` | `POST /consultations/{id}/refer` |

Son flujos **separados**. El botón de interconsulta va **dentro del Pool** (modo browse); "Agendar
con especialista" abre el Pool en **modo referral** ("Referir aquí") → motivo+fecha+firma; "Agendar
seguimiento" es su propio botón. Los dos de agenda comparten el modelo unificado (cita = fila en
`consultations` con `scheduled_at`+`parent_consultation_id`+status `scheduled`; cadena padre→hijas +
`GET /consultations/{id}/chain`).

## Reglas de la interconsulta

- La crea el **médico que atiende** la consulta (`consultations.assigned_doctor_id`).
- Invita a **UN** médico del pool. Por ahora: **1 interconsulta por consulta** (`consultation_id`
  único en la tabla).
- Ambos médicos comparten el **mismo `video_room_url`** → ambos ven al paciente en vivo.
- El **médico que atiende** ve todo (la consulta no cambia para él).
- El **médico invitado** ve datos **LIMITADOS** — solo lo clínicamente relevante, sin identidad:
  - motivo (`consultations.chief_complaint`)
  - notas (`consultations.internal_note` + `consultations.clinical_notes`)
  - **edad** del paciente (`patients.age_range`)
  - el `video_room_url` para unirse
  - **NUNCA**: nombre, cédula, teléfono, zona afectada, ni cualquier otro PII del paciente.

## Datos e historial

- Tabla `interconsultations`: `id`, `consultation_id` (FK, único), `invited_doctor_id` (user_id),
  `created_by_id` (quien atiende), `status`, `note?`, `created_at`, `updated_at`.
- Historial (MVP): la fila persiste (quién invitó a quién, cuándo) + entrada en `audit_log`
  (`interconsultation.created`).

## Dónde se ve

- **Asignar**: dentro del `DoctorPoolModal` (frontend), botón por médico, en
  `/panel-medico/consulta/[id]`.
- **Médico invitado**: una **sección nueva** en `/panel-medico` ("Interconsultas asignadas a mí")
  con la vista limitada + botón "Unirse a videoconsulta".
