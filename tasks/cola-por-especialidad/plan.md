# Implementation Plan: Cola por especialidad, derivación y sala de espera en vivo

> Spec: [`spec.md`](./spec.md) · Checklist: [`todo.md`](./todo.md)
> Repos: `api-medicos-por-venezuela` (rama `feat/cola-por-especialidad-y-derivacion` desde `dev`)
> y `medicos-por-venezuela` (misma rama desde `dev_aws`).

## Overview

Seis fases. Las cuatro primeras son backend y dejan la API completa y probada (verificable por
Swagger y tests) antes de tocar la UI: el frontend desplegado hoy sigue funcionando contra la API
nueva salvo por la toma por WhatsApp, que pasa a 422 a propósito. Las dos últimas son el
frontend y la marca.

```
Migración (is_placeholder · specialty_queue_access · derived_from_specialty_id ·
           requested_specialty · datos "Otra")
    │
    ├── services/queue_access.py  (R1: una sola función ver/tomar)
    │        ├── get_panel · claim (+ sala atómica) · GET /queue · /queue/take
    │        └── derive (cola) · refer_to_queue (detalle) · derivation targets
    │
    ├── services/waiting_room.py  (R5: estado + cadena + SSE)
    │
    └── doctors: requested_specialty · specialty-requests (admin)
                                   │
        Frontend: lib → panel (cola, KPIs, aviso) → detalle → sala/mi-caso → perfil/admin
                                   │
                              Marca (tokens + barra con logo) → E2E
```

## Architecture Decisions

### 1. Reglas del catálogo en columnas y tablas, no en nombres

"Otra" se marca con `specialties.is_placeholder` y los accesos extra (Psiquiatría→Psicología,
Medicina interna→Medicina general) viven en `specialty_queue_access(specialty_id,
extra_specialty_id)`, sembrada por nombre **una vez** en la migración. Es la convención del repo
(ver `20260831_220711_excluir_otra_del_selector_de_interconsulta.sql`): un literal en el código se
rompe en silencio con un renombre del catálogo.

### 2. Una sola función de visibilidad, en SQL

`queue_access.visible_specialty_ids(session, principal)` devuelve `None` (todas) o un conjunto de
ids (posiblemente vacío). `get_panel`, `claim`, `derive`, `GET /queue` y `/queue/take` la usan.
Pasa a filtrar en SQL (`specialty_id IN (...)`) en vez de Python: la cola ya no es "corta" (61 en
espera en prod) y el conjunto es la misma fuente para listar y tomar, así que no hay dos reglas
que se desincronicen. `can_attend_consultation` (mental vs física) queda sin uso y se elimina.

### 3. La sala se crea en el claim, no antes

`UPDATE … SET video_room_url = coalesce(video_room_url, :sala)` en el mismo UPDATE atómico del
claim. Crear la sala en el cliente antes del claim (lo de hoy) es lo que dejaba casos tomados sin
sala cuando esa llamada fallaba y se tragaba el error.

### 4. Derivar desde la cola muta el mismo caso; desde el detalle crea una hija

- Desde la cola nadie atendió al paciente: no hay acto médico que conservar, así que cambia la
  especialidad del mismo registro (escritura condicional sobre la especialidad que vio el médico,
  para que dos derivaciones simultáneas no se pisen).
- Desde el detalle hubo atención firmada: el padre se cierra como `referred_to_specialist` y una
  hija en `waiting` entra a la cola destino, heredando `queued_at`. Reusa la cadena
  (`parent_consultation_id`) que ya usan la agenda y `/chain`.
- `derived_from_specialty_id` (columna nueva) alimenta "Derivado desde X" en la cola sin leer
  eventos; el motivo y el autor salen del evento `derived` (una sola copia del motivo).

### 5. SSE con sesiones cortas y fallback a JSON

- El stream no usa `Depends(get_db)` para el bucle: retendría una conexión del pool por paciente
  en espera. Autoriza con la sesión de la request y consulta con `AsyncSessionLocal()` por ciclo,
  a través de una dependencia (`get_session_factory`) que los tests pueden sustituir.
- `fetch` con `ReadableStream` en el cliente (no `EventSource`): permite mandar el token en la
  cabecera `X-Consultation-Token` en vez de en la URL (el repo ya evita tokens en logs de proxy).
- Caddy hace flush inmediato de `text/event-stream`; se añade `X-Accel-Buffering: no` y
  `Cache-Control: no-cache` por si hay otro proxy.
- Duración máxima configurable + reconexión del cliente: ningún stream vive indefinidamente.
- Sin rate limit en estos GET (slowapi no tiene límite por defecto): detrás de Caddy el cupo por
  IP es compartido (ver memoria del proyecto) y una sala de espera no debe agotarlo.

### 6. El token del paciente sigue la cadena

El token de la consulta original autoriza leer el estado de sus descendientes (mismo paciente,
hacia abajo, nunca hacia arriba ni a primos). Cuando el caso vigente es otro, la respuesta trae un
token nuevo para él: sin eso, el paciente derivado no podría marcar `entered-call` en la hija.

### 7. Marca por tokens, no por página

Se introducen `--brand`, `--brand-light`, `--brand-dark`, `--navy` en `:root` y los componentes
de acción (`.btn-primary`, `.btn-outline`, `.link-button`, foco, `.hero`) pasan a `--brand`.
`--green` queda para estados de éxito. Una barra `components/PanelHeader.tsx` (navy + logo) se
monta en panel médico, detalle, perfil, agenda, interconsultas, mis pacientes, sala de espera y
Mi caso; `AdminLayout` pinta su lateral en navy con el logo. Nunito Sans pasa a ser la fuente de
`body`.

## Fases

### Fase 1 — Datos y regla de visibilidad (API)

1. Migración única `cola_por_especialidad` (idempotente).
2. Modelos: `Specialty.is_placeholder`, `SpecialtyQueueAccess`, `Consultation.derived_from_specialty_id`,
   `Doctor.requested_specialty(_at)`.
3. `services/queue_access.py` + tests de R1 (admin, Luis, Otra, sin especialidad, exacta, extras).
4. `get_panel` y `claim` sobre la función; `GET /queue`, `/queue/take` filtrados.
5. Claim con sala atómica, sin WhatsApp; `ensure_video_room` para `in_progress` sin sala.
6. `create_consultation` rechaza especialidad de relleno; `SpecialtyResponse.is_placeholder`.

Checkpoint: suite verde, `migrate` limpio sobre la base local con backup.

### Fase 2 — Derivación (API)

7. `GET /consultations/derivation-targets` (especialidades con médico habilitado).
8. `POST /{id}/derive` (cola) + correo.
9. `POST /{id}/refer-to-queue` (detalle) + correo.
10. `derivation` en el detalle; `derived_from_specialty` y `queued_at` en los ítems del panel.

### Fase 3 — Sala de espera (API)

11. `services/waiting_room.py`: resolver caso vigente por la cadena + fase.
12. `GET /{id}/waiting-room` y `/waiting-room/stream` (SSE) + tests (JSON por endpoint, generador
    como unidad).

### Fase 4 — Especialidad solicitada (API)

13. `PATCH /doctors/me` con `requested_specialty`; `GET /auth/me` o `/doctors/me` expone el
    estado para el aviso.
14. `GET /doctors/specialty-requests` + `POST /doctors/{id}/specialty-request/resolve`.

Checkpoint: cobertura ≥95%, ruff limpio, Swagger con `summary`/`responses`.

### Fase 5 — Frontend funcional

15. `lib/`: claim sin cuerpo, derive, refer-to-queue, targets, waiting room (stream + fallback),
    requested specialty, specialty requests.
16. Panel: tarjeta (especialidad, derivado desde, "Atender paciente", "Derivar a especialista"),
    `DerivarEspecialidadModal` + `ConfirmDialog`, KPIs, aviso "Otra", sin WhatsApp.
17. Detalle: botón pool renombrado, "Derivar con especialista" (especialidad → motivo → firma),
    bloque de derivación, "Unirse" que crea sala si falta.
18. Sala de espera y Mi caso en vivo.
19. Registro de paciente sin "Otra"; perfil con "Mi especialidad no está en la lista"; admin con
    aviso y resolución.

### Fase 6 — Marca, E2E y docs

20. Tokens de marca + `PanelHeader` + `AdminLayout`.
21. E2E nuevos y actualizados.
22. `changeslog.md`, CLAUDE.md/AGENTS.md (frontend) y README/.knowledge (API) donde describen lo
    que cambió.

## Riesgos

| Riesgo                                                                         | Mitigación                                                                                                                                        |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Especialidades sin médicos: casos que nadie ve                                 | El destino de derivar exige ≥1 médico habilitado; los admins médicos siguen viendo todo.                                                          |
| Un deploy de API antes que el frontend rompe el botón WhatsApp del panel viejo | Es el comportamiento buscado (422 con mensaje claro); el botón de video sigue funcionando porque el claim sigue aceptando `{via_whatsapp:false}`. |
| Streams abiertos agotan conexiones                                             | Sesiones cortas por ciclo, duración máxima, cierre en `ready`/`finished`.                                                                         |
| Supabase local no replica Realtime                                             | La sala de espera no depende de Realtime (SSE contra la API), así que los E2E sí la ejercitan.                                                    |
