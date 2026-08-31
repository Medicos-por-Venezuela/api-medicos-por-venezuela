# TODO: Interconsulta asíncrona

> Spec: [`spec.md`](./spec.md) · Plan y criterios de aceptación completos: [`plan.md`](./plan.md)
> API → rama base `dev` · Frontend → rama base `dev_aws`

## Fase 1 — Fundación de datos (API)

- [x] **T1** Migración: `patients.created_by_doctor_id` + `phone_whatsapp`/`affected_zone`
      nullable con CHECK · *auditar antes los usos de `phone_whatsapp`* — S
- [x] **T2** Migración + modelo `interconsultation_requests` (CHECKs de `mode`/`status`/
      `target_doctor_id`, 4 índices, RLS deny-all) · *4 estados:
      `open`/`taken`/`closed`/`cancelled` + `closed_at` + `closing_note`* — S
- [x] **T3** Migración `specialties.available_for_interconsultation` + Medicina general en
      `false` + filtro en `GET /specialties` · *sin literales en el código* — M
- [x] **T4** Migración: seed de `interconsultation_requests.write` y `.take` → doctor/admin/
      super_admin — XS

### ✅ Checkpoint 1

- [x] `python artisan migrate` limpio en base nueva **y** con datos restaurados
- [x] Suite completa verde (0 fallos) — nada existente roto
- [x] `ruff check` / `ruff format` limpios

## Fase 2 — El médico registra su paciente

- [x] **T5** Service + schemas: alta/listado/edición/soft-delete de pacientes propios
      (`consent=true` obligatorio, pertenencia) — M · *dep: T1*
- [x] **T6** Router `/doctors/me/patients` + tests **con IDOR cubierto** — M · *dep: T5*

### ✅ Checkpoint 2

- [x] Alta por Swagger sin WhatsApp ni zona afectada
- [x] El paciente no aparece en la cola pública ni para otro médico
- [x] Cobertura ≥95% en lo tocado

## Fase 3 — Solicitar y difundir

- [x] **T7** `mail`: stream `bulk=True` + `bcc` + `send_bulk` en lotes de 50 con tope
      `INTERCONSULTATION_FANOUT_MAX` · *sandbox debe seguir ganando sobre bulk* — S
- [x] **T8** Eventos `interconsultation_request_broadcast` y `..._taken` en
      `NOTIFICATION_EVENTS` · *test: el correo no lleva PII del paciente* — S · *dep: T7*
- [x] **T9** Service `create` (ambos modos, 422 si la especialidad no es elegible, 403 si el
      paciente no es suyo, `commit()` explícito) — M · *dep: T2, T3, T5, T8*
- [x] **T10** Router `POST /interconsultation-requests` + `GET /mine` — **congela el
      contrato del frontend** — M · *dep: T9*

### ✅ Checkpoint 3

- [x] Envío en vivo verificado (2026-08-31): Mailtrap aceptó el mensaje real por el
      stream bulk con los 3 destinatarios en BCC, en **una** petición. Acotado a tres
      correos autorizados por el usuario.
- [ ] ⚠️ `.env` local tiene token real de Mailtrap y **sin** `MAILTRAP_INBOX_ID`: cualquier
      envío desde local sale de verdad, y la base tiene médicos reales del backup
      restaurado. Definir el inbox de sandbox antes de probar el fan-out contra datos
      reales de la base.
- [x] `notified_count` coincide con los elegibles
- [x] 🚀 **La fase 6 ya puede arrancar en paralelo** (contrato congelado en T10)

## Fase 4 — Bandeja y toma

- [ ] **T11** Service + schemas de `inbox` · *la frontera de datos vive en el schema
      Pydantic, no en un `del` manual* — M · *dep: T9*
- [ ] **T12** Service `take` con `with_for_update(nowait=True)` + contacto del tratante +
      audit + correo — M · *dep: T11*
- [ ] **T13** Router `inbox`/`take`/detalle + **test que recorre el JSON crudo y falla si
      aparece PII del paciente** — M · *dep: T12*
- [ ] **T14** Test de concurrencia: `asyncio.gather` → exactamente un 200 y un 409 — S · *dep: T13*

### ✅ Checkpoint 4

- [ ] End-to-end por Swagger: registrar → solicitar → tomar → ver contacto
- [ ] Ambos correos llegan a Inbucket
- [ ] El test de concurrencia pasa 10 corridas seguidas sin flakear

## Fase 5 — Cierre del backend

- [ ] **T15** `cancel` (200 `open` / 409 `taken` / 403 ajena) **y** `close` (200 sobre
      `taken`, 409 sobre `open`, **403 si lo intenta el especialista**) + tests — S · *dep: T13*
- [ ] **T16** `.knowledge/interconsultas.md` con los **cuatro** flujos +
      `_EXPECTED_PREFIXES` + Swagger completo — S · *dep: T15*

### ✅ Checkpoint 5 — PR a `dev`

- [ ] Suite verde, 0 fallos · cobertura ≥95% · `ruff` limpio
- [ ] `tests/test_interconsultations.py` pasa **sin haberse editado** (cero regresión en vivo)

## Fase 6 — Frontend (`dev_aws`)

- [ ] **T17** `lib/doctorPatients.ts` + `lib/interconsultationRequests.ts` · *los tipos
      anonimizados no declaran campos de PII* — S · *dep: T10, T13*
- [ ] **T18** `pages/panel-medico/mis-pacientes.tsx`: formulario corto + consentimiento +
      listado — M · *dep: T17*
- [ ] **T19** Solicitar interconsulta: especialidad por defecto (**sin Medicina general**),
      médico específico como opción secundaria con su teléfono — M · *dep: T17*
- [ ] **T20** Bandeja del especialista + Tomar (409 → "otro colega ya lo tomó" + refresco) +
      pestaña "casos **activos** que tomé" (no es el historial) + "mis solicitudes" con
      cancelar y **cerrar** · *el botón de cerrar no existe para el especialista* — M · *dep: T19*
- [ ] **T21** E2E Playwright · *assert: la bandeja no contiene el nombre del paciente* — S · *dep: T20*
- [ ] **T22** Entrada en `changeslog.md` — XS · *dep: T21*

### ✅ Checkpoint 6 — PR a `dev_aws`

- [ ] Los 9 criterios de éxito de la spec, verificados uno por uno
- [ ] `pnpm build` (con `NEXT_DIST_DIR=.next-e2e`), `lint`, `tsc --noEmit`, `test:e2e` verdes

---

## Fuera de alcance (iteración siguiente)

- Historial y métricas de casos cerrados (`closing_note` ya se guarda para alimentarlo)
- Expiración o re-difusión de solicitudes que nadie toma
- `release`: que el especialista suelte un caso tomado
