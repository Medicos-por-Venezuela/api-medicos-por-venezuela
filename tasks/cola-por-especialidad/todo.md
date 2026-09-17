# TODO: Cola por especialidad, derivación y sala de espera en vivo

> Spec: [`spec.md`](./spec.md) · Plan: [`plan.md`](./plan.md)

## Fase 1 — Datos y regla de visibilidad (API)

- [x] T1 Migración `cola_por_especialidad` (is_placeholder, specialty_queue_access + seed,
      derived_from_specialty_id, requested_specialty, "Otra" en espera → Medicina general)
- [x] T2 Modelos ORM
- [x] T3 `services/queue_access.py` + tests R1
- [x] T4 `get_panel`/`claim`/`GET /queue`/`/queue/take` con la regla
- [x] T5 Claim con sala atómica, sin WhatsApp; `ensure_video_room` para `in_progress`
- [x] T6 `POST /consultations` rechaza "Otra"; `is_placeholder` en el catálogo

## Fase 2 — Derivación (API)

- [x] T7 `GET /consultations/derivation-targets`
- [x] T8 `POST /consultations/{id}/derive` + correo
- [x] T9 `POST /consultations/{id}/refer-to-queue` + correo
- [x] T10 `derivation` en detalle; `derived_from_specialty`/`queued_at` en panel

## Fase 3 — Sala de espera (API)

- [x] T11 `services/waiting_room.py`
- [x] T12 `GET /{id}/waiting-room` + `/stream` (SSE)

## Fase 4 — Especialidad solicitada (API)

- [x] T13 `PATCH /doctors/me` con `requested_specialty`
- [x] T14 `GET /doctors/specialty-requests` + resolver

### Checkpoint API

- [x] 0 failed, cobertura ≥95%, ruff limpio, migrate limpio

## Fase 5 — Frontend funcional

- [x] T15 `lib/`
- [x] T16 Panel (tarjeta, derivar, KPIs, aviso "Otra")
- [x] T17 Detalle (pool, derivar con especialista, bloque derivación, unirse)
- [x] T18 Sala de espera y Mi caso en vivo
- [x] T19 Registro sin "Otra", perfil con especialidad escrita, admin resuelve

## Fase 6 — Marca, E2E y docs

- [x] T20 Marca (tokens, PanelHeader, AdminLayout)
- [x] T21 E2E
- [x] T22 changeslog + docs

### Checkpoint final

- [x] tsc, lint, format, build, E2E verdes (49/49) · API 633 tests, cobertura 98%
- [x] QA manual en navegador (cola, derivar, sala de espera en vivo, marca)

## Fase 7 — Varias especialidades por médico

- [x] T23 Migración `doctor_specialties` + backfill
- [x] T24 `services/doctor_specialties.py` + `queue_scope` con `groups`
- [x] T25 `specialty_ids` en el perfil y `queues[]` en el panel
- [x] T26 Perfil con casillas, panel con una card por cola, fuera "Disponibilidad"
- [x] T27 Tests (API + E2E)

### Checkpoint Fase 7

- [x] API 640 tests verdes · E2E 50/50 · tsc, lint, format limpios
