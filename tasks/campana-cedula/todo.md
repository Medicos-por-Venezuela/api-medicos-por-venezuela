# Todo: comando de campaña para pedir la cédula

> Spec: [`spec.md`](./spec.md) · Plan: [`plan.md`](./plan.md)
> Verificación: `pytest` · `ruff format --check .` · `ruff check .`

---

## Fase 1 — Cimientos

### T1: Migración `credential_reminders`

**Descripción:** La tabla que hace posible reintentar sin duplicar, reenviar solo a quien no
completó, y medir la conversión de verdad.

**Acceptance criteria:**

- [ ] Columnas: `id`, `user_id` (FK a `users`), `sent_at`, `segment`, `batch_id`
- [ ] Índice **único** sobre `(user_id, sent_at::date)`: un correo por persona y día, aunque el lote se reintente
- [ ] Índice por `batch_id` para poder auditar un lote entero
- [ ] Idempotente (`create table if not exists`), como el resto de migraciones del repo

**Verification:**

- [ ] `python artisan migrate` aplica limpio
- [ ] `python artisan migrate:status` la lista como aplicada
- [ ] Insertar dos veces el mismo `user_id` el mismo día falla por el índice único

**Dependencies:** Ninguna
**Files:** `db/migrations/<fecha>_create_credential_reminders.sql`, `src/models/`
**Scope:** S (2 archivos)

---

### T2: El aviso entra en el catálogo de notificaciones

**Descripción:** `credential_reminder` con canal `email` en `NOTIFICATION_EVENTS`, para que la
campaña pase por `should_notify()` como cualquier otro aviso y sea desactivable.

**Acceptance criteria:**

- [ ] `NOTIFICATION_EVENTS["credential_reminder"] = ("email",)`
- [ ] Aparece en el catálogo que devuelve `GET /notification-prefs`
- [ ] Un médico puede desactivarlo y `should_notify` devuelve False
- [ ] Por defecto (sin preferencia guardada) devuelve True, igual que el resto

**Verification:**

- [ ] `pytest tests/test_notification_prefs.py`
- [ ] Manual: `GET /notification-prefs` con un token de médico lo incluye

**Dependencies:** Ninguna
**Files:** `src/services/notifications.py`, `tests/`
**Scope:** XS (1-2 archivos)

---

### ✅ Checkpoint A

- [ ] Migración aplicada; el índice único rechaza el duplicado
- [ ] El evento es visible y desactivable desde la API
- [ ] `pytest` verde

---

## Fase 2 — Selección y comando

### T3: Servicio de selección de destinatarios

**Descripción:** Quién entra en la campaña, por segmento, con las exclusiones aplicadas y
**contabilizadas** (un destinatario descartado en silencio es un bug que nadie ve).

**Acceptance criteria:**

- [ ] Base: médicos con ficha viva, sin cédula, cuenta anterior al 2026-07-14
- [ ] Segmentos: `sin-licencia` (~762), `licencia-numerica` (~1621), `texto-libre` (~464), `todos`
- [ ] Excluye y **cuenta por separado**: ya tiene cédula, `active=false`, ficha borrada, sin email, opt-out, ya avisado hace menos de `REENVIO_DIAS`
- [ ] Devuelve el desglose además de la lista: el resumen es el producto, no un efecto secundario
- [ ] Una sola consulta para la selección; nada de N+1 sobre ~3000 filas

**Verification:**

- [ ] `pytest` — un caso por exclusión
- [ ] Los conteos del dry-run cuadran con una consulta SQL escrita a mano
- [ ] La suma de incluidos + excluidos = el total de la base

**Dependencies:** T1, T2
**Files:** `src/services/credential_campaign.py` *(nuevo)*, `tests/`
**Scope:** M (3-4 archivos)

---

### T4: Comando `doctors:pedir-cedula`

**Descripción:** El CLI. Dry-run por defecto; `--enviar` para mandar de verdad.

**Acceptance criteria:**

- [ ] `python artisan doctors:pedir-cedula` imprime el desglose y **no envía nada**
- [ ] `--segmento`, `--limite`, `--enviar`
- [ ] Sin `--enviar`, `send_mail` no se llama **ni una vez**
- [ ] Con `--enviar`, revalida cada ficha **en el momento del envío** (pudo completarla desde el dry-run)
- [ ] Solo registra en `credential_reminders` los envíos que Mailtrap aceptó (`send_mail` → True)
- [ ] Espacia los envíos; no dispara ~2800 correos seguidos
- [ ] Al terminar imprime: enviados, fallidos y descartados por motivo
- [ ] `artisan help` lo documenta junto a los de migración

**Verification:**

- [ ] `pytest` con `send_mail` mockeado
- [ ] Manual: dry-run contra la BD local y comparar con SQL
- [ ] `ruff format --check` · `ruff check`

**Dependencies:** T3
**Files:** `artisan`, `scripts/campaign.py` *(nuevo)*, `tests/`
**Scope:** M (3 archivos)

---

### ✅ Checkpoint B — enseña sin enviar

- [ ] Test que demuestra que sin `--enviar` no hay ni una llamada a `send_mail`
- [ ] Los conteos cuadran con SQL a mano
- [ ] **Revisar contigo antes de escribir el correo**

---

## Fase 3 — Correo y red de seguridad

### T5: Tests del comando

**Descripción:** Fijar lo que no puede romperse nunca: que no se envíe por accidente y que no se
envíe dos veces.

**Acceptance criteria:**

- [ ] **El más importante:** sin `--enviar`, cero llamadas a `send_mail`
- [ ] Las cinco exclusiones, una por test
- [ ] Idempotencia: dos corridas seguidas no escriben dos veces al mismo
- [ ] `send_mail` devuelve False → **no** se registra, y ese médico reaparece en el siguiente lote
- [ ] `--limite` respeta el tope; `--segmento` filtra bien
- [ ] **Sanity check:** el test del dry-run debe ponerse rojo si se invierte el flag. Si no falla, no está probando nada

**Verification:**

- [ ] `pytest` verde
- [ ] Sanity check ejecutado de verdad, no asumido

**Dependencies:** T4
**Files:** `tests/test_credential_campaign.py` *(nuevo)*
**Scope:** S (1 archivo)

---

### T6: Plantilla del correo · **requiere tu aprobación**

**Descripción:** El texto. Se redacta un borrador, pero **no sale nada sin tu visto bueno**: es la
voz del proyecto hacia sus médicos voluntarios.

**Acceptance criteria:**

- [ ] Explica **por qué** se pide la cédula (verificar la credencial contra SACS/FPV), no solo qué hacer
- [ ] Enlaza directo a `/panel-medico/perfil`
- [ ] Versión texto y HTML
- [ ] Menciona cómo dejar de recibir estos avisos (el evento es desactivable)
- [ ] Sin PII más allá del nombre del propio destinatario
- [ ] **Tres decisiones tuyas antes de enviar:** el texto, si se anuncia fecha límite, y el remitente

**Verification:**

- [ ] Ensayo contra el inbox de Mailtrap (`MAILTRAP_INBOX_ID`) con `--limite 5`
- [ ] Los 5 llegan y se registran 5 filas
- [ ] Revisado por ti antes de cualquier destinatario real

**Dependencies:** T4
**Files:** `src/services/credential_campaign.py`, plantillas
**Scope:** S

---

### ✅ Checkpoint C — listo para el ensayo

- [ ] Los 9 criterios de éxito del spec, uno a uno
- [ ] Ensayo en el inbox de Mailtrap correcto
- [ ] `select count(*) from doctors where verified` idéntico antes y después
- [ ] `pytest` verde; `ruff` limpio
- [ ] **El envío real a 2846 personas lo autorizas tú. No se hace desde aquí.**
