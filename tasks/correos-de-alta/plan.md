# Plan: Correos de alta (pacientes y médicos)

> Fase 2 de 4. Spec: `tasks/correos-de-alta/spec.md`. Tareas: `tasks/correos-de-alta/todo.md`.
> Rama propuesta: `feat/correos-de-alta` sobre `dev`.

## Estrategia: tres rebanadas verticales, cada una entregable sola

No se construye "primero toda la infraestructura y luego los correos". Cada rebanada deja un
correo **funcionando de punta a punta y probado**, así que si el trabajo se corta a la mitad lo
entregado sirve igual.

El orden no es por dificultad sino por **valor y riesgo**: la rebanada 1 es la que resuelve el
"atenderlos más rápido" que motivó el pedido y es la única que no necesita tocar dominio
existente; la 3 es la que más código ajeno mueve y va al final, cuando el módulo de correo ya
está probado.

```
Rebanada 1: paciente nuevo (A)          ── no toca doctors.py ── entregable sola
        │
        ▼
Rebanada 2: registro de médico (B/C/D/E) ── requiere motivo de rechazo
        │
        ▼
Rebanada 3: aprobación del admin (E)     ── requiere ApprovalResult
```

## Grafo de dependencias

```
[1] config MAIL_INTERNAL_RECIPIENTS ──┐
                                      ├──> [3] registration_mail: correo A ──> [4] wiring en consultations.py
[2] módulo registration_mail (base) ──┘

[5] CredentialCheck.reason (dominio) ──> [6] correos B/C/D ──> [7] wiring en doctors.py (registro)

[8] ApprovalResult (dominio) ──────────> [9] correo E ──────> [10] wiring en doctors.py (approve)
```

`[1]` y `[2]` son hojas y pueden hacerse en cualquier orden. `[5]` y `[8]` tocan ambos
`services/doctors.py`: **van secuenciales**, no en paralelo, para no pelearse el fichero.

## Rebanada 1 — Aviso de paciente nuevo (correo A)

**Entrega:** operación recibe el correo con teléfono y enlace en cuanto entra alguien a la cola.

1. **Config.** `MAIL_INTERNAL_RECIPIENTS: str = ""` en `src/core/config.py`, con una propiedad
   `internal_mail_recipients -> list[str]` que parte por comas. Se copia el patrón exacto de
   `BACKEND_CORS_ORIGINS`/`cors_origins`, que ya resuelve listas en este repo — pydantic-settings
   no parsea `list[str]` desde `.env` sin ayuda, y no es este el cambio donde descubrir eso.
   **Vacío por defecto**, ver la decisión 3 del spec.
2. **Módulo `src/services/registration_mail.py`.** Estructura en tres capas, que es la que ya
   usa `notifications.py`:
   - `_build_*(...) -> tuple[str, str, str]` — puras (asunto, texto, html). Sin sesión, sin IO:
     son las que se prueban a fondo, incluida la aserción negativa de PII.
   - `*_mail_args(session, obj) -> dict | None` — resuelven con la **sesión viva** y devuelven
     valores planos. `None` significa "no hay a quién escribir", que no es un error.
   - `send_*(...)` — llaman a `mail.send_mail`, best-effort.
3. **Wiring en `routers/consultations.py`.** `create_consultation` gana `BackgroundTasks` y
   encola A. Dos guardas, ambas en `new_patient_mail_args`:
   - `patient.created_by_doctor_id is not None` → no se envía (paciente de consultorio).
   - `consultation.scheduled_at is not None` → no se envía (cita agendada; ya tiene su correo).
4. **Tests.** Composición + PII negativa + los dos filtros + resiliencia (`send_mail` lanzando
   y el endpoint devolviendo 201 igual).

**Checkpoint:** `uv run pytest tests/test_registration_mail.py -v` verde y un `POST
/consultations` manual contra el sandbox de Mailtrap mostrando el correo con el enlace bueno.

## Rebanada 2 — Registro de médico (correos B, C, D)

**Entrega:** cada registro avisa hacia dentro y le contesta al médico con el motivo real.

5. **`CredentialCheck.reason` en `services/doctors.py`.** Se añade un `str | None` con los cinco
   valores del spec, y `_verify_credential` deja de colapsar todos los caminos en `_UNVERIFIED`.
   Cambio de dominio **aislado y probado por su cuenta** antes de que ningún correo lo use: es
   la pieza con más riesgo de regresión de las tres, porque toca el camino por el que hoy pasa
   cada registro.
   - `_UNVERIFIED` deja de ser una constante compartida y pasa a `_unverified(reason)`. Hay que
     revisar sus usos actuales uno por uno; son pocos pero cada uno es un camino distinto.
   - **Comprobado (2026-09-03): `sacs_service` y `psicologo_service` YA distinguen los casos**,
     pero solo como prosa en un campo `error: str | None` ("La cédula no está registrada en el
     SACS" vs "Error de conexión con el SACS" vs "Error HTTP del SACS: 503"...). La información
     existe; lo que falta es un canal tipado. Se añade `error_kind: str | None` a
     `SacsVerificationResponse` y `PsicologoVerificationResponse`, fijado en cada `_fallo`.
     **No** se hace string-matching sobre esos mensajes: son texto de cara al humano, cambian
     con cualquier retoque de redacción y romperían el motivo en silencio.
6. **Correos B/C/D** en `registration_mail.py`, con `DOCTOR_REJECTION_REASONS: dict[str, str]`
   mapeando motivo → frase en español. El diccionario es la fuente única: el test recorre sus
   claves y exige que cada una produzca un texto distinto, así que añadir un motivo sin su
   frase rompe la suite.
7. **Wiring en `routers/doctors.py`** (`register_doctor`). Encola B+D o C+E según
   `doctor.verified`.

**Checkpoint:** suite verde + los cinco motivos produciendo cinco textos distintos.

## Rebanada 3 — Aprobación del admin (correo E)

**Entrega:** el médico se entera de que ya puede entrar, sin correos duplicados.

8. **`ApprovalResult` en `services/doctors.py`.** `approve_doctor` pasa a devolver
   `NamedTuple(doctor, newly_approved)`. Radio de impacto verificado: **un solo caller**
   (`routers/doctors.py:328`) y ningún test lo llama directo (van todos por HTTP).
9. **Correo E + resolución de destinatario.** `doctors.email` → si falta, `users.email` vía
   `user_id` → si tampoco, no se envía y queda un warning. Aprobar a un médico sin correo es
   una acción válida.
10. **Wiring en `routers/doctors.py`** (`approve_doctor`): encola E **solo si**
    `newly_approved`.

**Checkpoint:** aprobar dos veces manda **un** correo; aprobar a un médico sin email en ninguna
tabla no envía y no rompe.

## Riesgos

| # | Riesgo | Probabilidad | Mitigación |
|---|---|---|---|
| R1 | **Escribirle a Oriana de verdad desde un entorno de pruebas.** Basta un `.env` local con token de Mailtrap para que el primer registro de prueba le llegue. | Media | `MAIL_INTERNAL_RECIPIENTS` vacío por defecto (decisión 3 del spec) + `MAILTRAP_INBOX_ID` en local. Se define solo en `.env.production`. |
| R2 | ~~`_verify_credential` no puede distinguir "SACS caído" de "no encontrado".~~ **RESUELTO al planificar.** Ambos servicios ya distinguen los casos; solo que en prosa (`error: str`). | ~~Alta~~ → **Baja** | Se añade `error_kind` tipado a los dos schemas de respuesta y se fija en cada `_fallo`. Cambio pequeño y simétrico. Prohibido string-matchear los mensajes en español: son texto para humanos y cambian sin avisar. |
| R3 | **`POST /consultations` es público y con rate limit por IP (10/min).** Un bot distribuido podría inundar el Gmail de Oriana. | Baja | Se documenta; no se mitiga en este alcance. Si preocupa, la opción barata es un tope por hora de avisos internos en memoria del proceso. **Decisión tuya al revisar este plan.** |
| R4 | Romper tests existentes al cambiar `approve_doctor`. | Baja | Radio verificado: un caller, cero tests directos. Se corre `tests/test_doctor_approval.py` justo después. |
| R5 | El `BackgroundTask` intenta usar la sesión ya cerrada. | Media | El patrón `*_mail_args` resuelve todo dentro de la request y pasa valores planos. Es la razón de que esa capa exista y no se compongan los correos en el background. |

## Qué NO se hace

- La campaña a los ~2 900 médicos del backlog (va directa por Mailtrap, fuera de la API).
- Cualquier envío retroactivo o backfill.
- Correos a pacientes de consultorio.
- Plantillas HTML con diseño: se sigue el estilo sobrio de `notifications.py` (texto + HTML
  mínimo). Un sistema de plantillas es otra tarea, y este no es el momento de estrenarlo.
- Tocar el frontend.

## Verificación final (Definition of Done del repo)

```bash
uv run pytest --cov=src --cov-report=term-missing --asyncio-mode=auto   # verde, ≥95 %
uv run ruff check . --fix && uv run ruff format .                        # limpio
```

Más: prueba manual contra el sandbox de Mailtrap de los cinco correos, y `.env.production`
documentado con `MAIL_INTERNAL_RECIPIENTS` (si no, en producción no sale ninguno de los tres
avisos internos — es el precio consciente de R1).
