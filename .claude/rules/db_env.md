# Reglas de Base de Datos y Concurrencia Avanzada

## 🌐 Gestión de Entornos
- **Local:** **Supabase local real** (CLI: `npx supabase start`), NO un Postgres propio —
  mismo Postgres/Auth/Realtime que usa el frontend (puerto `54322`; API/Auth/REST en
  `54321`). Ver README -> "Supabase local (desarrollo)" para el setup completo. Las
  credenciales se leen desde `.env` mediante `POSTGRES_*`.
- **Producción:** Supabase (Connection Pooler en puerto 5432/6543 según tipo de sesión). Las mutaciones en tablas críticas disparan eventos nativos por CDC (Change Data Capture) hacia el Board.
- **JWT:** local firma con claves asimétricas (ES256 vía JWKS); prod hoy usa HS256 con
  secreto compartido. `src/core/security.py::decode_token` soporta ambos (mira el `alg`
  del token) — ver `SUPABASE_JWKS_URL`/`SUPABASE_JWT_SECRET` en `.env.example`.

## 🔒 Control de Concurrencia Crítica (Fallo Rápido / Nowait)
- **Bloqueo Pesimista Obligatorio:** Para evitar condiciones de carrera cuando dos médicos hacen clic en el mismo paciente en el mismo milisegundo, **NUNCA** realices un `select` común seguido de un `update`.
- **Uso de `nowait=True`:** Usa siempre `with_for_update(nowait=True)` en SQLAlchemy para bloquear la fila inmediatamente. Si la fila ya está bloqueada por otra transacción activa, la base de datos lanzará inmediatamente una excepción `OperationalError`, evitando que la petición HTTP se quede colgada esperando.

  ```python
  # Patrón de código requerido para transacciones de cola concurrentes:
  from sqlalchemy.exc import OperationalError

  try:
      stmt = (
          select(ColaPacientes)
          .where(ColaPacientes.id == paciente_id, ColaPacientes.estado == "esperando")
          .with_for_update(nowait=True)
      )
      result = await session.execute(stmt)
      turno = result.scalar_one_or_none()

      if not turno:
          raise HTTPException(status_code=404, detail="El turno ya no está disponible.")

      turno.estado = "en_consulta"
      turno.medico_id = medico_id
      await session.commit()
  except OperationalError:
      await session.rollback()
      raise HTTPException(
          status_code=409,
          detail="Este paciente está siendo seleccionado por otro médico en este momento. Inténtalo de nuevo."
      )
  ```

## ✅ Implementación real en este repo (lee esto antes de tocar la cola)

El patrón de arriba es la guía; así está **realmente** implementado (respeta esto):

- **Modelo real:** la "cola" es la tabla `consultations` con `status == "waiting"` (no existe
  `ColaPacientes`/`estado`). Al tomar un caso: `status -> "in_progress"`,
  `assigned_doctor_id`, `opened_at`/`started_at = now()`.
- **Separación capas:** el `select ... with_for_update(nowait=True)` y el `commit` viven en
  `src/services/queue.py` (capa de negocio). El servicio **lanza excepciones nativas**
  (no `HTTPException`). El router `src/routers/queue.py` mapea el error a HTTP.
- **⚠️ asyncpg ≠ psycopg2:** con el driver **asyncpg**, una fila bloqueada NO llega como
  `OperationalError`, sino como `sqlalchemy.exc.DBAPIError` cuyo `sqlstate` es **`55P03`**
  (`LockNotAvailableError`). El `except OperationalError` del ejemplo clásico **no lo captura**.
  Usa el helper `src.core.exceptions.is_lock_not_available(exc)` (detecta `55P03` venga de
  asyncpg o psycopg2) para traducirlo a `409`.
- **Manejo global:** además del catch específico en el router de la cola, hay manejadores
  globales en `src/core/exceptions.py` para `OperationalError`, `IntegrityError` y el lock
  `55P03` -> `409`.

## 🧬 Migraciones de esquema (con tracking)

Los cambios de esquema son archivos `.sql` en **`db/migrations/`**, gestionados por el CLI
**`scripts/migrate.py`** (Python, multiplataforma; usa `asyncpg` y la misma config que la app).
Registra lo aplicado en la tabla `schema_migrations` (`filename` PK, `applied_at`) y aplica **solo
lo pendiente**, en orden alfabético por nombre, cada migración en una transacción.

- **Invocación:** el CLI `artisan` de la raíz (estilo Laravel) es el frente recomendado y se
  auto-ejecuta con el python del `.venv`: `python artisan migrate` / `migrate:status` /
  `"make:migration" "<desc>"`. Equivale a `scripts/migrate.py up|status|new` (invocable directo).
- Para crear una migración usa `make:migration` (o `migrate.py new`), no escribas el archivo a mano.
- **Conexión:** de `DATABASE_URL` o las piezas `POSTGRES_*` (igual que la app). Para prod, exporta
  `DATABASE_URL` de Supabase antes de `up`.
- El contenedor de Postgres **no** aplica migraciones (no tiene Python): tras `docker compose up`
  se corre `migrate.py up` desde el host/CI. No reintroduzcas loops de migración en `db/init/`.

- **Convención obligatoria de cada migración:** transaccional (nada de `CREATE INDEX
  CONCURRENTLY`) e **idempotente** (`IF NOT EXISTS` / `ON CONFLICT`), para que aplicarla
  sobre una base restaurada de backup que ya la contenga sea un no-op seguro.
- **Nombres:** prefijo ordenable (`NNN_` o fecha `AAAAMMDD_`); el orden de aplicación es el
  del nombre. Coordinar la numeración entre ramas para no colisionar.
- **Nunca** apliques migraciones a mano con `psql -f` suelto: rompe el tracking. Usa el runner
  (`python artisan migrate`), sea contra Supabase local (dev) o Supabase (prod).
- `scripts/load_local.sh` (restaura un backup de prod en el Postgres de Supabase LOCAL) ya
  delega en el runner para aplicar el delta pendiente después de restaurar; no reintroduzcas
  loops ad-hoc sobre `db/migrations/`. `db/init/` (stubs del viejo Postgres propio) ya no existe:
  Supabase local trae su propio esquema `auth` real.

## 🔌 Driver async y pooler de Supabase

- Driver: **asyncpg** (`postgresql+asyncpg://`). asyncpg **no** entiende `?sslmode=` en la URL;
  el SSL se pasa por `connect_args` (ver `src/core/config.py`).
- Detrás del **transaction pooler** de Supabase (puerto `6543`, PgBouncer) hay que desactivar
  los prepared statements: `connect_args={"statement_cache_size": 0,
  "prepared_statement_cache_size": 0}`. Ya está parametrizado por entorno.
- **Local (dev):** Postgres 17 en Docker (`docker compose`), datos reales restaurados desde un
  backup de Supabase. **Producción:** Supabase vía `DATABASE_URL`/`POSTGRES_*`.
