# Interconsultas y derivaciones

Cuatro flujos distintos para "que otro médico participe en este caso". Se parecen en el nombre y
no se parecen en nada más. Confundirlos ya causó problemas cuando eran tres; ahora son cuatro.

## Distinción clave (¡importante!) — CUATRO flujos

| | **Interconsulta en vivo** | **Interconsulta asíncrona** | **Agendar seguimiento** | **Agendar con Especialista** |
|---|---|---|---|---|
| De dónde sale el paciente | La **cola pública** | El **consultorio** del médico (no está en la plataforma) | La cola pública | La cola pública |
| Hay consulta abierta | **Sí**, sigue abierta | **No existe consulta** | Se **cierra** (`closed`) | Queda **`referred_to_specialist`** |
| A quién le llega | A **UN** médico que elige | A **todos** los de una especialidad (o a uno elegido) | Al **mismo** médico | A **otro** médico (referencia) |
| Cuándo | Ahora, en vivo | Cuando el especialista entre | Otro día | Otro día |
| Cómo se conectan | **Video compartido** | **Fuera de la plataforma** (WhatsApp/correo) | Consulta agendada | Consulta agendada |
| Qué ve el otro | Datos **limitados** (motivo/notas/edad) | Datos **limitados** + contacto del tratante **al tomar** | — | **Notas previas** (chain) |
| Endpoint | `POST /interconsultations` | `POST /interconsultation-requests` | `POST /consultations/{id}/schedule-follow-up` | `POST /consultations/{id}/refer` |
| Tabla | `interconsultations` | `interconsultation_requests` | `consultations` (hija) | `consultations` (hija) |

Los dos de agenda comparten el modelo unificado (cita = fila en `consultations` con
`scheduled_at` + `parent_consultation_id` + status `scheduled`; cadena padre→hijas +
`GET /consultations/{id}/chain`).

**En la UI llevan nombres distintos a propósito.** "Interconsulta" a secas es ambiguo entre los
dos primeros: el botón del Pool es la de **video en vivo**; la del panel de pacientes propios es
**pedir segunda opinión**.

---

## 1. Interconsulta EN VIVO (segunda opinión durante una consulta activa)

- La crea el **médico que atiende** la consulta (`consultations.assigned_doctor_id`).
- Invita a **UN** médico del pool. Por ahora: **1 interconsulta por consulta**
  (`consultation_id` único en la tabla).
- Ambos médicos comparten el **mismo `video_room_url`** → ambos ven al paciente en vivo.
- El **médico que atiende** ve todo (la consulta no cambia para él).
- El **médico invitado** ve datos **LIMITADOS** — solo lo clínicamente relevante, sin identidad:
  - motivo (`consultations.chief_complaint`)
  - notas (`consultations.internal_note` + `consultations.clinical_notes`)
  - **edad** del paciente (`patients.age_range`)
  - el `video_room_url` para unirse
  - **NUNCA**: nombre, cédula, teléfono, zona afectada, ni cualquier otro PII del paciente.

### Datos e historial

- Tabla `interconsultations`: `id`, `consultation_id` (FK, único), `invited_doctor_id` (user_id),
  `created_by_id` (quien atiende), `status`, `note?`, `created_at`, `updated_at`.
- Historial (MVP): la fila persiste (quién invitó a quién, cuándo) + entrada en `audit_log`
  (`interconsultation.created`).

### Dónde se ve

- **Asignar**: dentro del `DoctorPoolModal` (frontend), botón por médico, en
  `/panel-medico/consulta/[id]`.
- **Médico invitado**: una **sección** en `/panel-medico` ("Interconsultas asignadas a mí")
  con la vista limitada + botón "Unirse a videoconsulta".

---

## 2. Interconsulta ASÍNCRONA (paciente de consultorio)

Spec completa: `tasks/interconsulta-asincrona/spec.md`.

Existe porque la de arriba solo sirve para pacientes de la cola pública, con los dos médicos
conectados a la vez. La mayor parte de la práctica de un médico son sus pacientes de consultorio,
que nunca van a estar en la plataforma.

### El flujo

1. El médico **registra a su paciente** (`POST /doctors/me/patients`). Formulario corto: nombre,
   edad, alergias, antecedentes. **No** pide teléfono ni zona afectada — nadie de la plataforma
   va a contactar a ese paciente. Sí exige `consent = true`: el médico atestigua que su paciente
   autorizó compartir el caso.
2. **Solicita la interconsulta** (`POST /interconsultation-requests`) en uno de dos modos:
   - `specialty` (el principal): elige la especialidad y se difunde por correo a **todos** los
     médicos habilitados de esa especialidad.
   - `doctor`: elige a un médico concreto; solo a él le llega, y el tratante ve su teléfono desde
     el principio.
3. Los especialistas ven el caso **anonimizado** en su bandeja (`GET .../inbox`).
4. El **primero que lo toma gana** (`POST .../{id}/take`) y recibe el contacto del tratante.
5. El tratante recibe un correo de que su caso fue tomado y ve al especialista en `GET .../mine`.
6. **Hablan por fuera** (WhatsApp/correo). La plataforma hace el match, no la conversación.
7. El **tratante** cierra el caso (`POST .../{id}/close`), o lo cancela si nadie lo tomó
   (`POST .../{id}/cancel`).

### Máquina de estados

```
                  (el tratante cancela)
      ┌───────────────────────────────────► cancelled
      │
    open ─────────────────────────────► taken ─────────────────────────► closed
           (el especialista TOMA:                 (el MÉDICO TRATANTE
            carrera, gana uno solo)                cierra, nunca el
                                                   especialista)
```

- `open` **no expira ni se re-difunde**: espera hasta que alguien la tome.
- **Un solo especialista** por caso. De ahí la carrera y el 409.
- El especialista **no cierra ni suelta** el caso. Cerrar es del tratante: es quien sabe si la
  ayuda sirvió.

### Qué ve el especialista

| | Antes de tomar | Después de tomar |
|---|---|---|
| Del caso | motivo, notas clínicas, **rango etario** | lo mismo |
| Del paciente | **nada más** | **nada más** |
| Del médico tratante | **nada** | nombre, WhatsApp y correo |

**NUNCA**, en ningún estado: nombre, cédula, teléfono, correo, zona ni descripción del paciente.
La frontera vive en los **schemas Pydantic** (`InterconsultationRequestInbox` /
`...Taken`): los campos prohibidos no están declarados, así que no pueden escaparse aunque la
query los traiga. Hay un test que recorre el JSON crudo y falla si aparece cualquiera.

Ocultar también la identidad del **tratante** antes de tomar es deliberado: que el caso se elija
por el caso y no por quién pregunta.

### Concurrencia

`take` bloquea la fila SI sigue `open` con `with_for_update(nowait=True)`, igual que la cola
(`services/queue.py`). Lo que compra el `nowait` no es solo evitar la doble asignación: es que el
perdedor reciba **409 de inmediato** en vez de quedarse esperando el commit del otro. En un panel
con médicos mirando, colgarse es peor que perder. Ver
`tests/test_interconsultation_requests_concurrency.py`.

### Correos (fan-out)

- Eventos: `interconsultation_request_broadcast` y `interconsultation_request_taken`
  (`NOTIFICATION_EVENTS`, canal email, con opt-out).
- **Destinatarios**: médicos activos, verificados y con ficha válida para ejercer — el mismo
  criterio del gate de credencial. No se le difunde un caso a quien el backend no dejaría
  atenderlo. Se excluye al solicitante.
- **Envío por el stream BULK de Mailtrap, en lotes por BCC** (`mail.send_bulk`). Una especialidad
  puede tener cientos de médicos y el SDK es síncrono: un correo por cabeza serían cientos de
  peticiones secuenciales. BCC y no `to` múltiple porque los correos de los médicos son datos de
  colegas. Tope en `MAIL_FANOUT_MAX`, y superarlo **loguea** el recorte.
- `notified_count` guarda a cuántos elegibles se les avisó: distingue "no me llegó" de "no eras
  destinatario".
- ⚠️ En local, sin `MAILTRAP_INBOX_ID` los correos salen **de verdad**, y la base suele tener
  datos restaurados de producción. Definí el inbox de sandbox antes de probar el fan-out.

### Por qué tabla nueva y no `interconsultations`

Aquella exige `consultation_id` NOT NULL + UNIQUE: nace de una consulta activa. Este flujo no
tiene consulta. Compartir tabla habría obligado a aflojar esa FK y a que cada query distinguiera
los dos casos.

### Catálogo

`specialties.available_for_interconsultation` decide qué se puede pedir. **Medicina general está
en `false`**: pedirle ayuda a otro general no es una interconsulta. La regla vive en la columna,
nunca en un literal del código — es el cuarto nombre de especialidad que habría quedado
hardcodeado en este repo (ver la migración `20260813_142814`).

### Dónde se ve

- **Registrar paciente y solicitar**: `/panel-medico` (pacientes propios).
- **Bandeja del especialista** y **casos activos que tomé**: `/panel-medico`.
- Los correos enlazan a `/panel-medico` (base configurable en `FRONTEND_URL`).

---

## 3 y 4. Los dos flujos de agenda

- **Agendar seguimiento**: el mismo médico, otro día. Cierra la consulta actual y crea una hija.
- **Agendar con Especialista**: otro médico, otro día. La consulta queda `referred_to_specialist`
  y el especialista recibe las **notas previas** (transfiere el cuidado, no pide una opinión).

"Agendar con especialista" abre el Pool en **modo referral** ("Referir aquí") → motivo + fecha +
firma. "Agendar seguimiento" es su propio botón.
