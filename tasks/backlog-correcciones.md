# Backlog de correcciones — API

Hallazgos de la lectura del repo del 2026-09-29 (Kelly, encargo de mensajería por Workana) y
observaciones del cliente. Ninguno está autorizado todavía: cada línea necesita confirmación del
cliente y horas asignadas antes de tocarse (skill `corregir-bug`). Prioridad: **A** afecta
seguridad o datos, **B** afecta operación, **C** deuda de documentación o limpieza.

| # | Pri | Qué | Dónde | Estimación |
|---|---|---|---|---|
| C-01 | A | La suite de tests **no corre en CI** (solo `--collect-only`): el esquema local carece de la FK `public.users.id → auth.users.id` que prod sí tiene; al corregirla quedaron ~67 fallos sin diagnosticar. Decidir si los helpers crean la fila en `auth.users` o se asume la diferencia. | `.github/workflows/ci.yml:517-541`, `tests/_helpers.py` | 6–10 h |
| C-02 | A | `GET /consultations` y `GET /consultations/{id}` siguen abiertos a cualquier staff, sin acotar por especialidad (la cola sí lo está). | `.knowledge/business-logic.md:135-139`, `src/routers/consultations.py:267,512` | 3 h |
| C-03 | A | `.env` local con token real de Mailtrap y sin `MAILTRAP_INBOX_ID`: cualquier prueba con datos del backup envía correos reales (riesgo R1 abierto; el cliente confirmó que Mailtrap es producción). | `tasks/interconsulta-asincrona/todo.md`, `.env.example` | 0,5 h + política |
| C-04 | B | No hay worker ni cron real que invoque `POST /queue/release-stale`; las consultas estancadas solo se liberan si un admin lo llama. | `src/routers/queue.py:150`, `.knowledge/business-logic.md` | 2 h (cron en EC2 igual que los recordatorios) |
| C-05 | B | Rate limiting in-memory por proceso (`slowapi`); con varios workers cada uno lleva su cuenta. Pendiente Redis o gateway. | `.claude/rules/security.md:469` | 3 h |
| C-06 | B | El comentario de CI habla de 269 tests / 96 %; hoy hay ~718 funciones de test y 98 %. | `.github/workflows/ci.yml` | 0,25 h |
| C-07 | C | README y `.knowledge/business-logic.md` listan como existentes endpoints que ya no están: `POST /consultations/{id}/heartbeat`, `POST /queue/attend-next`, `POST /profiles/me/online`, `GET /specialties/catalog`. | `README.md:621-647`, `.knowledge/business-logic.md:117-124` | 0,5 h |
| C-08 | C | `tasks/datos-emergencia-y-direccion-cifrada/todo.md` con 17 casillas sin marcar aunque el trabajo está mergeado (PR #110) y la dirección se retiró después (PR #125/#126). | `tasks/datos-emergencia-y-direccion-cifrada/` | 0,25 h |
| C-09 | C | `docs/migracion-profiles-a-users.md` dice «plan aprobado, pendiente de ejecutar» cuando las migraciones ya se aplicaron y la vista se dropeó (`20260802_105108`). | `docs/migracion-profiles-a-users.md` | 0,25 h |
| C-10 | C | RPC y columna vestigiales en la base: `mark_myself_online()`, `mark_patient_waiting()`, `mark_patient_wants_whatsapp()`, `profiles.last_seen_at`. Nadie las usa; limpieza con migración. | `db/migrations/000_core_schema.sql` | 1 h |
| C-11 | C | Migración `20260705_094821` deja anotado «pendiente aparte: sincronizar este mismo add-column». Verificar si ya se hizo. | `db/migrations/20260705_094821_add_article8_columns_to_users.sql:12` | 0,25 h |
| C-12 | C | Rama `docs/spec-campana-cedula` con spec y plan de la campaña para pedir la cédula, sin ejecutar. Confirmar si sigue en agenda. | `origin/docs/spec-campana-cedula` | — |

## Observaciones del cliente (no confirmadas como encargo)

- «La videollamada funciona no todo el tiempo» (Ori, 21-sep). Jitsi auto-alojado en
  `meet.medicosporvenezuela.org`. Antes de tocar nada: recoger casos concretos (navegador,
  red, hora) y revisar el servidor de Jitsi. Fuera del alcance de mensajería.
