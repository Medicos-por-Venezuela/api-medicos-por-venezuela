# API Médicos por Venezuela

API REST construida con **FastAPI + SQLAlchemy + Pydantic**.

- **Desarrollo local:** Postgres en Docker, con los **modelos y datos reales** traídos
  desde Supabase mediante un backup.
- **Producción:** se conecta a **Supabase** (Postgres) vía variables de entorno.

Los modelos reflejan el esquema **real y actual** de Supabase (12 tablas: `profiles`,
`patients`, `doctors`, `consultations`, `consultation_events`, `prescriptions`,
`referrals`, `rest_notes`, `treatment_plans`, `follow_ups`, `messages`, `admin_users`).

## Stack

- **FastAPI (async)** — framework web + OpenAPI automático; todas las rutas son `async def`.
- **SQLAlchemy 2.0 async** — ORM con driver **asyncpg** (`AsyncSession`).
- **Pydantic v2** + **pydantic-settings** — validación (`EmailStr`, `Field`) y configuración.
- **PostgreSQL 17** — local en Docker (dev) / Supabase (prod).
- **uv** (Astral) — gestor de paquetes y entornos.
- **pytest + pytest-asyncio** — suite async con cobertura (objetivo ≥95%).
- **Ruff** — lint + formato.
- **Docker / Docker Compose** — `db` (Postgres) + `api`.

## Arquitectura (capa de servicios, 3-tier)

```
src/
├── main.py            # App FastAPI, CORS, registro de manejadores globales
├── core/
│   ├── config.py      # Settings por entorno (URL async, SSL, pooler)
│   ├── errors.py      # Excepciones de dominio (NotFound/Conflict/BadRequest/...)
│   └── exceptions.py  # Manejadores globales -> respuestas HTTP semánticas
├── db/                # Base declarativa + engine/sesión async
├── models/            # Capa de BD: modelos ORM (12 tablas reales)
├── schemas/           # Capa de validación: Pydantic (Create/Update/Response)
├── services/          # Capa de negocio: lógica + queries + bloqueos (async)
└── routers/           # Capa HTTP: routers DELGADOS que delegan en services
db/init/               # Init del Postgres local: stubs de Supabase + restore del backup
backups/               # Backups de Supabase (.dump/.sql) — IGNORADO por git (PII)
scripts/               # backup_supabase.sh / load_local.sh
tests/                 # Suite async (CRUD aislado por savepoints + concurrencia)
```

- **Routers** (capa HTTP): reciben la petición, inyectan `AsyncSession` con `Depends`
  y delegan en el servicio. Sin lógica ni queries.
- **Services** (capa de negocio): funciones async puras; reciben `session` y datos planos,
  ejecutan SQLAlchemy y lanzan **excepciones de dominio** (no `HTTPException`).
- Los **manejadores globales** (`core/exceptions.py`) traducen las excepciones de dominio
  y las nativas de SQLAlchemy (`OperationalError`, `IntegrityError`, lock `55P03`) a HTTP.

## Inicio rápido (desarrollo local con Docker)

```bash
cp .env.example .env          # valores local por defecto (Postgres local)
docker compose up --build     # levanta Postgres + API
```

En el **primer arranque**, el contenedor de Postgres:

1. ejecuta `db/init/00-supabase-stubs.sql` (crea el esquema `auth`, `auth.uid()` y los
   roles `anon`/`authenticated` que el dump de Supabase necesita para restaurarse), y
2. ejecuta `db/init/01-restore-from-backup.sh`, que restaura el backup más reciente de
   `./backups/*.dump` **con datos** (usa `--disable-triggers` para que las FKs a
   `auth.users` y el trigger de códigos no interfieran).

Luego:

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health
- Postgres local: `localhost:5432` (db `medicos`, user `postgres`, pass `localdev`)

> Sin backup en `./backups`, la base se crea solo con los stubs (vacía); puedes cargar
> datos después con `scripts/load_local.sh`.

### Recargar / reiniciar la base local

```bash
# Recargar el backup más reciente en la base que ya corre:
./scripts/load_local.sh

# Empezar de cero (borra el volumen y vuelve a restaurar en el arranque):
docker compose down -v && docker compose up -d
```

## Ejecutar la API sin Docker (contra el Postgres local) — con `uv`

```bash
uv sync --extra dev                       # crea el entorno e instala dependencias
uv run uvicorn src.main:app --reload      # http://localhost:8000
```

> Necesitas un Postgres local en `localhost:5432`. La forma más simple es levantarlo
> con `docker compose up -d db` (incluye el restore del backup).

## Backups de Supabase

`scripts/backup_supabase.sh` genera un dump (esquema + datos) en `./backups` usando
`pg_dump` desde un contenedor `postgres:17`, conectándose por el **session pooler
(puerto 5432)**. Requiere las credenciales en `.env.supabase` (no se versiona).

```bash
./scripts/backup_supabase.sh
```

> ⚠️ Los backups contienen **PII real** (pacientes, perfiles). El directorio `backups/`
> está en `.gitignore` y **nunca** debe subirse al repositorio.

## Configuración (variables de entorno)

`DATABASE_URL` tiene prioridad; si no, se arma desde las piezas `POSTGRES_*`.

| Variable               | Local (dev)      | Producción (Supabase)                       |
| ---------------------- | ---------------- | ------------------------------------------- |
| `POSTGRES_HOST`        | `localhost`      | `aws-1-us-east-1.pooler.supabase.com`       |
| `POSTGRES_PORT`        | `5432`           | `6543` (transaction pooler)                 |
| `POSTGRES_DB`          | `medicos`        | `postgres`                                  |
| `POSTGRES_USER`        | `postgres`       | `postgres.<project_ref>`                    |
| `POSTGRES_PASSWORD`    | `localdev`       | (secreto de Supabase)                       |
| `POSTGRES_SSLMODE`     | `prefer`         | `require`                                   |
| `SUPABASE_JWT_SECRET`  | (dev por defecto)| **secreto JWT de Supabase** (obligatorio)   |
| `JITSI_DOMAIN`         | `meet.jit.si`    | dominio Jitsi (self-host opcional)          |
| `STALE_CONSULTATION_MINUTES` | `30`       | umbral para liberar consultas estancadas    |
| `BACKEND_CORS_ORIGINS` | `*`              | dominios del frontend                       |

- **Local:** `.env` (copiado de `.env.example`).
- **Producción:** `.env.supabase` (ignorado por git) o el gestor de secretos del hosting.
  ⚠️ `SUPABASE_JWT_SECRET` es **obligatorio** en producción (Supabase → Settings → API → JWT Secret);
  el valor por defecto del código es solo para desarrollo/pruebas.

## Autenticación y autorización (RBAC)

El login sigue en **Supabase Auth**; el frontend manda el JWT como
`Authorization: Bearer <token>`. La API valida firma/exp/audiencia, saca el `sub`
(= id del perfil) y carga el rol desde `profiles`. Roles (valores reales en BD):
`patient | doctor | specialist | admin | super_admin` (medico=`doctor`, paciente=`patient`).

| Grupo  | Roles | Puede |
| ------ | ----- | ----- |
| público | (sin token) | crear paciente/consulta, heartbeat, sala de video, `GET /specialties`, `GET /specialties/catalog` |
| **staff** | doctor, specialist, admin, super_admin | cola, tomar/atender, leer/editar consultas, cerrar, eventos, listar pacientes/médicos |
| **admin** | admin, super_admin | listar perfiles, revocar médico, CRUD médicos/especialidades, editar/borrar paciente, liberar estancadas |
| self | el titular del JWT | `GET /auth/me`, `POST /profiles/me/online`, `POST /profiles/me/finalize-role` |

- El **actor** de las acciones (médico que toma/cierra) se toma del JWT, **no** de ids del cliente (anti-IDOR).
- Un paciente autenticado solo ve **sus** consultas; un médico **revocado** (`active=false`) pierde acceso al instante.

## Endpoints (prefijo `/api/v1`)

| Método  | Ruta                                | Descripción                          |
| ------- | ----------------------------------- | ------------------------------------ |
| `GET`   | `/auth/me`                          | Perfil del usuario autenticado       |
| `GET`   | `/queue`                            | Board: consultas en espera (staff)   |
| `POST`  | `/queue/{id}/take`                  | Toma atómica de una consulta (200/409/404) |
| `POST`  | `/queue/attend-next`                | "Atender al siguiente" (selección + toma atómica) |
| `POST`  | `/queue/release-stale`              | Liberar consultas estancadas (admin/CRON) |
| `GET`   | `/specialties`                      | Lista pública de especialidades activas |
| `GET`   | `/specialties/catalog`              | Catálogo de necesidades + reglas de matching |
| `POST/PATCH/DELETE` | `/specialties` / `/specialties/{id}` | CRUD de especialidades (admin/super_admin) |
| `POST`  | `/consultations/{id}/close`         | Cerrar / no-show (+ evento de auditoría) |
| `POST`  | `/consultations/{id}/heartbeat`     | Presencia del paciente (sala de espera) |
| `POST`  | `/consultations/{id}/video-room`    | Sala Jitsi idempotente               |
| `POST`  | `/profiles/{id}/online`             | Presencia del médico (`last_seen_at`) |
| `PATCH` | `/profiles/{id}/active`             | Revocar / reactivar médico (admin)   |
| `POST`  | `/profiles/{id}/finalize-role`      | Finalizar rol (patient/doctor)       |
| `GET`   | `/patients`                         | Lista pacientes                      |
| `POST`  | `/patients`                         | Crea paciente (requiere `consent`)   |
| `GET`   | `/patients/{id}`                    | Detalle                              |
| `PATCH` | `/patients/{id}`                    | Actualiza                            |
| `DELETE`| `/patients/{id}`                    | Elimina                              |
| `GET`   | `/consultations?status=&patient_id=`| Lista consultas (filtros)            |
| `POST`  | `/consultations`                    | Crea consulta (código autogenerado)  |
| `GET`   | `/consultations/{id}`               | Detalle                              |
| `PATCH` | `/consultations/{id}`               | Actualiza estado/asignación/notas    |
| `GET`   | `/consultations/{id}/events`        | Eventos de la consulta               |
| `POST`  | `/consultations/{id}/events`        | Registra evento                      |
| `GET`   | `/doctors?status=`                  | Lista médicos                        |
| `POST`  | `/doctors`                          | Crea médico                          |
| `GET`   | `/doctors/{id}` · `PATCH` · `DELETE`| Detalle / actualizar / eliminar      |
| `GET`   | `/profiles?role=`                   | Lista perfiles (solo lectura)        |
| `GET`   | `/profiles/{id}`                    | Detalle de perfil                    |

## Concurrencia: toma de cola anti-colisión

`POST /queue/{id}/take` protege la integridad de la cola cuando dos médicos eligen el
mismo paciente a la vez. El servicio usa bloqueo pesimista de **fallo rápido**:

```python
select(Consultation).where(...status == "waiting").with_for_update(nowait=True)
```

- El **ganador** obtiene `200` y la consulta pasa a `in_progress`.
- El **perdedor** obtiene `409` (fila bloqueada, `LockNotAvailableError` / SQLSTATE `55P03`)
  o `404` si la carrera se resolvió justo tras el commit del ganador.
- Nunca hay doble asignación ni peticiones colgadas.

## Tests

```bash
uv run pytest --cov=src --cov-report=term-missing
```

- Aislamiento por **savepoints** (cada test mutativo hace rollback automático).
- Prueba de **colisión concurrente** con `asyncio.gather` + `httpx.AsyncClient`
  (un ganador 200, el resto 409/404; sin doble asignación).
- Cobertura actual: **97%** (objetivo ≥95%).
- Requiere el Postgres local corriendo (`docker compose up -d db`).

## Migración del frontend: Supabase → esta API

El objetivo es que la app Next.js haga **exactamente lo mismo** que hoy hace contra Supabase,
pero llamando a esta API. Equivalencias de las operaciones de datos:

| Operación actual (Supabase)                                  | Endpoint de esta API |
| ------------------------------------------------------------ | -------------------- |
| `insert patients`                                            | `POST /patients` |
| `insert consultations` (status waiting, priority derivada)   | `POST /consultations` (deriva categoría/prioridad del paciente) |
| update atómico de toma (`.eq('status','waiting')`)           | `POST /queue/{id}/take` · `POST /queue/attend-next` |
| update cierre / no-show + `addEvent`                         | `POST /consultations/{id}/close` |
| `insert consultation_events`                                 | `POST /consultations/{id}/events` |
| `rpc('mark_patient_waiting')`                                | `POST /consultations/{id}/heartbeat` |
| `rpc('mark_myself_online')`                                  | `POST /profiles/me/online` |
| `rpc('set_my_role')`                                         | `POST /profiles/me/finalize-role` |
| `update profiles set active` (revocar)                       | `PATCH /profiles/{id}/active` |
| `/api/videoconsulta` (sala Jitsi idempotente)                | `POST /consultations/{id}/video-room` |
| `getSession` + cargar profile                                | `GET /auth/me` |
| `select` (cola, detalle, mi-caso, dashboard)                 | `GET /queue`, `GET /consultations`, `GET /consultations/{id}`, `GET /profiles` |
| `SPECIALTY_NEEDS` / `canAttend` (lib/utils.ts)               | `GET /specialties/catalog` + ya aplicado server-side en `attend-next` |

> **Autenticación:** el login sigue en **Supabase Auth** (email/password + Google OAuth); el
> frontend manda el **JWT de Supabase** y la API lo valida + aplica RBAC (ver sección de arriba y
> `@.claude/rules/security.md`). Las acciones de médico (tomar/cerrar/presencia) derivan el actor del
> token, así que esas llamadas ya **no** mandan `assigned_doctor_id`/`closed_by`.

## Notas

- En local **no** se usa RLS: la autorización la impone la **capa de servicios/RBAC** (replica las
  políticas RLS). Las políticas RLS de Supabase se restauran pero no aplican al conectarse como dueño.
- **Observabilidad:** cada respuesta lleva `X-Correlation-ID` y los logs salen en **JSON** (logger
  `mpv.api`); prohibido `print()`. **Paginación:** las listas usan `limit`/`offset` (máx. 100).
- **Resiliencia:** `POST /queue/release-stale` devuelve a la cola las consultas `in_progress`
  abiertas hace más de `STALE_CONSULTATION_MINUTES`; pensado para un CRON/worker.
- El `code` de las consultas lo genera **siempre** el trigger `generate_consultation_code`
  (cualquier valor enviado por el cliente se ignora).
- Las tablas `prescriptions`, `referrals`, `rest_notes`, `treatment_plans`, `follow_ups`,
  `messages` y `admin_users` están **modeladas**; sus endpoints se pueden añadir según se
  necesiten.
