# Spec: Correos de alta (pacientes y médicos)

> Intención confirmada con el usuario vía `interview-me` (2026-09-03).
> Fases posteriores: `tasks/correos-de-alta/plan.md` y `todo.md`.
> Un solo repo: `api-medicos-por-venezuela`. **No toca el frontend.**

## Supuestos (corrígeme antes de que empiece)

1. **El correo A engancha en `POST /consultations`, no en `POST /patients`.** Es el supuesto
   con más consecuencias del spec, así que va primero. El alta de un paciente son DOS
   peticiones seguidas del formulario: primero `POST /patients` (la ficha) y luego
   `POST /consultations` (el caso, que es lo que entra a la cola). En la primera todavía no
   existen ni la especialidad solicitada ni el código del caso ni el enlace — o sea, casi todo
   lo que pediste que llevara el correo. Y una ficha sin consulta no es alguien esperando: es
   un formulario a medio enviar. Enganchar en la consulta es lo que hace verdad "llegó un
   paciente nuevo, atiéndanlo rápido".
2. **`orianaramirez@gmail.com` e `info@…` se configuran, no se escriben en el código**
   (`MAIL_INTERNAL_RECIPIENTS`). Cambiar a quién se avisa no debería ser un despliegue.
3. **Los correos al médico (D y E) no son opcionables.** No entran en el catálogo de
   `notification_prefs`: son correos de ciclo de vida de la cuenta, como un "restablece tu
   contraseña". Un médico no puede desactivar el aviso de que su registro fue rechazado, y de
   hecho al registrarse todavía no tiene preferencias que consultar.
4. **Nada retroactivo.** No hay backfill ni barrido de registros existentes. La única vía por
   la que un médico antiguo recibe correo es que un admin pulse "Aprobar" (correo E), que es
   lo que pediste explícitamente.
5. **Un correo caído nunca rompe un registro.** Regla ya vigente en `services/mail.py`
   (best-effort). Si Mailtrap falla, el paciente queda en la cola y el médico queda registrado.

## Objective

Que los tres eventos de alta salgan solos por correo, para que nadie tenga que entrar al panel
a descubrirlos, y que el médico rechazado reciba en el mismo instante la lista exacta de lo que
debe responder.

### Usuarios

| Actor | Qué gana |
|---|---|
| **Oriana / `info@`** (operación) | Se enteran de que entró un paciente sin vigilar el panel, y pueden contactarlo desde el propio correo. |
| **Médico que se registra** | Sabe si quedó habilitado o no, y si no, por qué y qué mandar — sin escribir a nadie para preguntarlo. |

### Por qué ahora

Hoy la única forma de saber que llegó alguien es abrir `/admin`. Para un paciente en cola eso
es tiempo de atención perdido. Para un médico bloqueado es peor: su solicitud se queda parada
y él no tiene forma de saber que le falta un papel — de ahí los **2 847 médicos sin cédula**
que hoy hay en la base, que nunca supieron que su registro no había terminado.

### Historias de usuario

1. Como operación, recibo un correo en cuanto un paciente entra a la cola pública, con su
   teléfono, para poder contactarlo sin abrir el panel.
2. Como operación, recibo un correo cuando un médico se registra y el SACS/FPV **no** lo
   valida, con su cédula y el motivo, para poder cotejar los papeles que me responda.
3. Como operación, recibo un correo cuando un médico se registra y el SACS/FPV **sí** lo
   valida, para saber que ya hay un profesional más habilitado.
4. Como médico no aprobado, recibo un correo que me dice con qué cédula quedé registrado, por
   qué no pasé, y que debo responder con mi título, mi licencia SACS y mi carta de artículo 8.
5. Como médico aprobado —por el registro automático o porque un admin me aprobó después—
   recibo un correo diciéndome que ya puedo usar la plataforma.

## Alcance: los cinco correos

| # | Se dispara cuando | Destinatario | Contenido |
|---|---|---|---|
| **A** | Se crea una consulta de la **cola pública** | `MAIL_INTERNAL_RECIPIENTS` | Nombre, WhatsApp, zona, especialidad solicitada, código y enlace al caso |
| **B** | `POST /doctors` y la credencial **no** valida | `MAIL_INTERNAL_RECIPIENTS` | Nombre, **cédula**, email, teléfono, tipo profesional, especialidad, **motivo**, fecha |
| **C** | `POST /doctors` y la credencial **sí** valida | `MAIL_INTERNAL_RECIPIENTS` | Lo mismo que B, sin motivo, indicando el registro oficial que lo validó |
| **D** | `POST /doctors` y la credencial **no** valida | **El médico** | Su cédula, el motivo, y los tres documentos a responder |
| **E** | `POST /doctors` con credencial válida, **o** `POST /doctors/{id}/approve` que cambie el estado | **El médico** | Que ya puede entrar, con enlace al panel |

Los correos B/C y D/E salen del **mismo** evento de registro: uno hacia dentro y otro hacia el
médico. La aprobación manual del admin dispara **solo E** — hacia dentro no se avisa, porque
quien lo aprobó estaba mirando la pantalla cuando lo hizo.

### Frontera de PII (decisión explícita, no accidental)

`orianaramirez@gmail.com` es un Gmail personal: lo que entre en ese correo queda fuera de la
plataforma, fuera del `audit_log` y en un buzón que la organización no controla.

| Dato | ¿Va al correo? | Por qué |
|---|---|---|
| Nombre, teléfono, zona del paciente | **Sí** | Es lo que permite contactarlo; sin ellos el correo no ahorra el viaje al panel |
| **Cédula del paciente** | **No** | No aporta a contactarlo |
| **Alergias, descripción clínica** | **No** | Historia clínica; se lee en el panel, que deja traza de quién la vio |
| **Cédula del médico** | **Sí**, en B y D | Es el ASUNTO del aviso: sin ella no se puede cotejar nada, y devolvérsela al médico es como detecta que la tecleó mal — la causa más común de que el SACS no lo encuentre |

## Tech Stack

Python 3.12 · FastAPI async · SQLAlchemy 2.0 (asyncpg) · Pydantic v2 · PostgreSQL 17
(Supabase) · Mailtrap (`mailtrap` SDK) · `uv` · Ruff 0.16.3 · pytest + pytest-asyncio.

**Sin dependencias nuevas.** Todo se construye sobre `services/mail.py`, que ya resuelve envío
best-effort, streams transaccional/bulk, sandbox local y categorías.

## Commands

```bash
uv sync --extra dev
uv run uvicorn src.main:app --reload --workers 1
uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto
uv run ruff check . --fix
uv run ruff format .
```

## Project Structure

```
src/services/registration_mail.py   → NUEVO. Composición y envío de los 5 correos
src/services/mail.py                 → sin cambios (se usa tal cual)
src/services/doctors.py              → CAMBIA: motivo de rechazo + resultado de approve
src/core/config.py                   → CAMBIA: MAIL_INTERNAL_RECIPIENTS
src/schemas/{sacs,psicologo}.py      → CAMBIA: error_kind tipado (motivo del rechazo)
src/services/{sacs,psicologo}.py     → CAMBIA: fija error_kind en cada _fallo
src/routers/consultations.py         → CAMBIA: encola A
src/routers/doctors.py               → CAMBIA: encola B/C/D en registro, E en approve
tests/test_registration_mail.py      → NUEVO
```

Módulo aparte y no dentro de `notifications.py` a propósito: ese fichero es el de las
**preferencias del médico** (catálogo opt-out, citas y recordatorios). Estos correos son de
ciclo de vida de la cuenta y no se pueden desactivar; meterlos ahí invitaría al siguiente
lector a añadirlos al catálogo y hacerlos opcionables por error.

## Cambios de dominio necesarios

### 1. El motivo del rechazo tiene que existir

Hoy `_verify_credential()` devuelve `CredentialCheck(verified=False)` para **cinco** caminos
distintos: sin tipo profesional, tipo inexistente, tipo sin registro verificable (p. ej.
nutricionista), el registro no encontró la cédula, y el registro respondió sin nombre o sin
licencia. Todos colapsan en el mismo `_UNVERIFIED`.

Sin distinguirlos, el correo D dice lo mismo a todo el mundo — y el correo cuyo propósito es
"indicar el porqué" no indicaría ningún porqué. Se añade un motivo al `CredentialCheck`:

| Motivo | Qué se le dice al médico |
|---|---|
| `sin_tipo` | Falta indicar el tipo profesional |
| `tipo_no_verificable` | Tu profesión no se valida en línea; la revisamos a mano |
| `no_encontrado` | No encontramos tu cédula en el SACS/FPV — revisa que esté bien escrita |
| `datos_incompletos` | El registro respondió sin licencia; hay que verificarla a mano |
| `servicio_no_disponible` | El registro oficial no respondió; lo revisamos a mano |

**`servicio_no_disponible` es el que justifica el trabajo.** Hoy un SACS caído produce
exactamente el mismo rechazo silencioso que una cédula falsa, y ni el médico ni la operación
pueden distinguirlos. Es fail-closed a propósito y así sigue — pero decirlo cambia lo que el
médico hace con el correo.

### 2. `approve_doctor` debe decir si cambió algo

El endpoint es idempotente: aprobar dos veces no falla. Sin distinguir "aprobado ahora" de
"ya estaba aprobado", dos clics del admin mandan dos correos E al médico. El servicio ya
calcula `was_verified` para el `audit_log`; se expone en el retorno
(`ApprovalResult(doctor, newly_approved)`) y el correo E sale **solo** cuando pasa de `False`
a `True`.

### 3. Destinatario del correo E cuando el médico es antiguo

`doctors.email` es nullable (las fichas backfilleadas no lo traen; el contacto vive en
`users`). Para E se resuelve `doctors.email` → si falta, `users.email` vía `user_id` → si
tampoco hay, **no se envía y se registra un warning**. No es un error: aprobar a un médico sin
correo es una acción válida, solo que no hay a dónde escribirle.

## Code Style

El patrón ya establecido en `notifications.py`: los datos se resuelven **dentro** de la
request (sesión viva) y se pasan planos al `BackgroundTask`, que corre después de cerrarla.

```python
async def new_patient_mail_args(session: AsyncSession, consultation: Consultation) -> dict | None:
    """Args del aviso interno de paciente nuevo, o None si no hay a quién avisar.

    Se resuelve con la sesión viva: el BackgroundTask corre tras cerrar la request y ya no
    puede consultar la base. Devuelve None —y no lanza— cuando no hay destinatarios internos
    configurados: no avisar es una configuración válida, no un fallo del alta.
    """
    if not settings.MAIL_INTERNAL_RECIPIENTS:
        return None
    patient = await session.get(Patient, consultation.patient_id)
    if patient is None or patient.created_by_doctor_id is not None:
        return None  # de consultorio: privado de su médico, nunca sale de la plataforma
    return {
        "patient_name": patient.full_name,
        "phone": patient.phone_whatsapp,
        "zone": patient.affected_zone,
        "specialty": await specialties_service.name_for_id(session, consultation.specialty_id),
        "code": consultation.code,
    }
```

Reglas: docstring que explique **por qué**, no qué; `summary` + `responses` en endpoints
nuevos (aquí no hay ninguno); nada de PII en los logs (solo categoría y tipo de error).

## Testing Strategy

pytest + pytest-asyncio, aislamiento por savepoints (`tests/conftest.py`), cobertura **≥95 %**.
Los tests **nunca** envían: `mail_enabled()` es `False` sin `MAILTRAP_API_TOKEN`, y además se
dobla `mail.send_mail` para capturar destinatario, asunto y cuerpo.

| Nivel | Qué cubre |
|---|---|
| Unidad | Composición de los 5 cuerpos: que D nombre los tres documentos y ambos correos de correo, que el motivo correcto aparezca por cada valor del enum, que el asunto identifique el evento |
| Unidad | **Frontera de PII**: aserción negativa de que el cuerpo de A **no** contiene cédula, alergias ni descripción del paciente |
| Integración | `POST /consultations` encola A; un paciente de consultorio **no** lo encola |
| Integración | `POST /doctors` encola B+D si no valida, C+E si valida |
| Integración | `POST /doctors/{id}/approve` encola E la primera vez y **no** la segunda (idempotencia) |
| Integración | Aprobar un médico sin email en ninguna de las dos tablas no envía y no rompe |
| Resiliencia | `send_mail` lanzando: el alta y la aprobación siguen devolviendo 201/200 |

La aserción negativa de PII es la que hace que la decisión sobreviva: cualquiera puede añadir
un campo "útil" al correo dentro de seis meses, y sin ese test nadie se entera.

## Boundaries

- **Siempre:** correo best-effort (nunca romper el flujo que lo dispara); resolver datos con
  la sesión viva y pasarlos planos al BackgroundTask; cobertura ≥95 %; `ruff check`/`format`
  limpios; asunto y cuerpo en español de Venezuela.
- **Preguntar antes:** añadir un campo nuevo al correo A (toca la frontera de PII acordada);
  meter estos correos en `NOTIFICATION_EVENTS`; cambiar los destinatarios por defecto;
  cualquier envío masivo o retroactivo.
- **Nunca:** mandar cédula, alergias o descripción clínica del paciente; escribir PII en los
  logs; enviar correo desde la capa de servicios de dominio (se encola en el router, como el
  resto del repo); tocar la campaña de los 2 900 médicos, que va por fuera.

## Success Criteria

1. Un alta real de la cola pública produce **un** correo a los destinatarios internos con
   nombre, WhatsApp, zona, especialidad y enlace al caso — y **sin** cédula ni alergias.
2. Un alta de paciente **de consultorio** no produce ningún correo.
3. Un registro de médico que el SACS/FPV no valida produce **dos** correos: el interno con
   cédula y motivo, y el del médico nombrando los tres documentos y las dos direcciones.
4. Un registro que sí valida produce **dos**: el interno y el de bienvenida al médico.
5. Pulsar "Aprobar" en el panel manda **un** correo al médico; pulsarlo otra vez **no** manda
   ninguno.
6. Con Mailtrap caído, el alta responde `201` y la aprobación `200`; el fallo solo deja log.
7. Los cinco motivos de rechazo producen cinco textos distintos en el correo D.
8. Suite verde, cobertura ≥95 %, `ruff` limpio.

## Decisiones cerradas en la revisión (2026-09-03)

1. **El correo A sale solo para la cola EN VIVO.** Las consultas agendadas
   (`scheduled_at is not None`) quedan fuera: ya tienen su propio correo por
   `notifications.send_appointment_email`, y meterlas aquí duplicaría el aviso.
2. **El enlace del correo A apunta a `/admin/pacientes`**, porque los destinatarios son
   operación y no el médico que atiende.
3. **`MAIL_INTERNAL_RECIPIENTS` viene VACÍO por defecto** y se define en `.env.production`.
   Es la misma filosofía de `MAILTRAP_API_TOKEN` en este repo ("apagado si no está
   configurado"): con el valor real por defecto, cualquier entorno de pruebas que tenga un
   token de Mailtrap le escribiría a Oriana de verdad al primer registro de prueba. El precio
   es que hay que acordarse de ponerlo al desplegar, y por eso va en Success Criteria.

## Ampliación 2026-10-16 — Correo F: confirmación al paciente

> Sale de la auditoría de notificaciones del 2026-10-16, junto con
> `tasks/avisos-chat-presencia/`. Aquélla cubre el chat; esto cubre el hueco del alta, que es
> de este spec.

**El hueco.** Los cinco correos de arriba resolvieron «que operación se entere» y «que el
médico sepa en qué quedó su registro». Al paciente no le llega **ninguno**: el único correo que
recibe al registrarse es el OTP de verificación (`verificacion-correo-registro`), que es un
trámite, no una confirmación. Sale del formulario con su caso en cola y la bandeja vacía.

Eso tiene una consecuencia que ya costó un correo nuevo una vez: el enlace a su sala de espera
vive **solo** en la pestaña que acaba de abrir. Quien la cierra —o entra, no hay nadie todavía
y se sale— no tiene forma de volver. Ése es literalmente el agujero que `notifications`
documenta haber tapado después con el aviso de videoconsulta
(«el correo de registro tampoco existía», `notifications.py:153-161`). El correo F lo tapa en
el origen, en vez de esperar a que un médico tome el caso para dar la primera señal de vida.

| # | Se dispara cuando | Destinatario | Contenido |
|---|---|---|---|
| **F** | Se crea una consulta de la **cola pública** (mismo evento que A) | **El paciente**, si tiene `patients.email` | Código del caso, especialidad, enlace a su sala de espera con token fresco, y la advertencia de urgencias |

### Criterios de aceptación

- **CA-F.1** Mismo disparador que A (`POST /consultations`) y mismas tres exclusiones: paciente
  de consultorio, consulta agendada (ya tiene el suyo por `notifications`), y —específica de
  F— paciente sin `email`, que es opcional en `patients`.
- **CA-F.2** El enlace es `build_waiting_room_url(consultation.id)`, con **token fresco**. No se
  reutiliza el que devuelve `POST /consultations`: F es un correo, y un correo se abre cuando
  se abre.
- **CA-F.3** Dice que **puede tardar** y que **no hace falta volver a registrarse**. Es el
  mismo texto que el correo de derivación ya usa, y por el mismo motivo: sin él, la espera se
  interpreta como un fallo y el paciente crea una segunda cuenta y un segundo caso.
- **CA-F.4** Lleva la advertencia de urgencias, como todos los correos al paciente de este
  sistema.
- **CA-F.5** **Sin nada clínico.** Ni el motivo de consulta, ni alergias, ni la descripción.
  Va la especialidad solicitada, que es lo mismo que A ya manda y que el paciente escribió él.
  El test negativo de PII cubre también F.
- **CA-F.6** Best-effort: si Mailtrap falla, el alta sigue devolviendo `201`.

### Por qué en `registration_mail.py` y no en `notifications.py`

Mismo criterio que los cinco anteriores (ver «Project Structure»): F es un correo de **ciclo de
vida**, no opcionable. Un paciente anónimo no tiene `Profile` y por tanto no tiene
`notification_prefs` que consultar — meterlo en el catálogo opt-out sería declarar una
preferencia que nadie puede ejercer.

### Success Criteria (añadidos)

9. Un alta real de la cola pública **con correo** produce **dos** correos: el interno (A) y el
   del paciente (F), con código, especialidad y enlace a su sala.
10. Un alta **sin** correo produce solo A, y no falla.
11. Un alta **de consultorio** no produce ni A ni F.
12. El cuerpo de F no contiene motivo, alergias ni descripción del caso.

## Open Questions

Ninguna.
