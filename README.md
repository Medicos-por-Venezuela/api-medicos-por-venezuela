# API Médicos por Venezuela

API REST construida con **FastAPI + SQLAlchemy + Pydantic**.

- **Desarrollo local:** Postgres en Docker, con los **modelos y datos reales** traídos
  desde Supabase mediante un backup.
- **Producción:** se conecta a **Supabase** (Postgres) vía variables de entorno.

Los modelos reflejan el esquema **real y actual** de Supabase (12 tablas: `profiles`,
`patients`, `doctors`, `consultations`, `consultation_events`, `prescriptions`,
`referrals`, `rest_notes`, `treatment_plans`, `follow_ups`, `messages`, `admin_users`).

## Stack

- **FastAPI** — framework web + OpenAPI automático.
- **SQLAlchemy 2.0** — ORM síncrono (driver `psycopg2`).
- **Pydantic v2** + **pydantic-settings** — validación y configuración por entorno.
- **PostgreSQL 17** — local en Docker (dev) / Supabase (prod).
- **Docker / Docker Compose** — `db` (Postgres) + `api`.

## Estructura

```
app/
├── main.py            # App FastAPI, CORS, routers, /health
├── core/config.py     # Settings por entorno (local-first)
├── db/                # Base declarativa + engine/sesión
├── models/            # Modelos ORM (12 tablas reales)
├── schemas/           # Esquemas Pydantic
├── crud/              # Acceso a datos
└── api/routers/       # Endpoints REST (/api/v1)
db/init/               # Init del Postgres local: stubs de Supabase + restore del backup
backups/               # Backups de Supabase (.dump/.sql) — IGNORADO por git (PII)
scripts/               # backup_supabase.sh / load_local.sh
```

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

## Ejecutar la API sin Docker (contra el Postgres local)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

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
| `BACKEND_CORS_ORIGINS` | `*`              | dominios del frontend                       |

- **Local:** `.env` (copiado de `.env.example`).
- **Producción:** `.env.supabase` (ignorado por git) o el gestor de secretos del hosting.

## Endpoints (prefijo `/api/v1`)

| Método  | Ruta                                | Descripción                          |
| ------- | ----------------------------------- | ------------------------------------ |
| `GET`   | `/patients`                         | Lista pacientes                      |
| `POST`  | `/patients`                         | Crea paciente (requiere `consent`)   |
| `GET`   | `/patients/{id}`                    | Detalle                              |
| `PATCH` | `/patients/{id}`                    | Actualiza                            |
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

## Notas

- En local **no** se usa RLS: la API se conecta como dueño de la base. Las políticas RLS y
  funciones de Supabase se restauran pero no afectan al acceso directo en dev.
- Las tablas `prescriptions`, `referrals`, `rest_notes`, `treatment_plans`, `follow_ups`,
  `messages` y `admin_users` están **modeladas**; sus endpoints se pueden añadir según se
  necesiten.
