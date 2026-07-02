#!/bin/bash
# Runner de migraciones con tracking.
#
# Aplica solo los .sql de db/migrations/ que aún NO se han aplicado, en orden por
# nombre, cada uno dentro de una transacción. Registra cada migración aplicada en
# la tabla `schema_migrations`, así una migración corre una sola vez sin importar
# cuántas veces se ejecute este script ni desde cuántas ramas.
#
# Requisito de las migraciones: deben ser transaccionales (nada de CREATE INDEX
# CONCURRENTLY) e idempotentes por convención (IF NOT EXISTS / ON CONFLICT), de
# modo que aplicarlas sobre una base restaurada de backup que ya las contenga sea
# un no-op seguro.
#
# Modos (cómo se conecta a Postgres):
#   (por defecto)  docker exec al contenedor local mpv-db   -> para devs (host)
#   --local        psql directo                             -> dentro del contenedor
#   --remote       psql "$DATABASE_URL"                      -> producción (Supabase)
#
# Uso:
#   scripts/migrate.sh                             # local, vía docker
#   DATABASE_URL="postgres://..." scripts/migrate.sh --remote   # producción
set -euo pipefail

MODE="${1:-docker}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/db/migrations}"
DB_CONTAINER="${DB_CONTAINER:-mpv-db}"
PGUSER="${POSTGRES_USER:-postgres}"
PGDATABASE="${POSTGRES_DB:-medicos}"

case "${MODE}" in
  docker)
    psql_exec() { docker exec -i "${DB_CONTAINER}" psql -U "${PGUSER}" -d "${PGDATABASE}" -v ON_ERROR_STOP=1 "$@"; } ;;
  --local)
    psql_exec() { psql -U "${PGUSER}" -d "${PGDATABASE}" -v ON_ERROR_STOP=1 "$@"; } ;;
  --remote)
    [ -n "${DATABASE_URL:-}" ] || { echo "[migrate] --remote requiere la variable DATABASE_URL" >&2; exit 1; }
    psql_exec() { psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 "$@"; } ;;
  *)
    echo "[migrate] modo desconocido: ${MODE} (usa: docker | --local | --remote)" >&2; exit 1 ;;
esac

# 1. Tabla de tracking (la primera vez).
psql_exec -q -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);"

# 2. Aplica cada migración pendiente, en orden, y la registra.
# La expansión glob de bash viene ordenada alfabéticamente (001_ antes que
# 20260630_...) y respeta rutas con espacios; nullglob la deja vacía si no hay .sql.
shopt -s nullglob
applied=0
for path in "${MIGRATIONS_DIR}"/*.sql; do
  name="$(basename "${path}")"
  if [ "$(psql_exec -tAc "SELECT 1 FROM public.schema_migrations WHERE filename = '${name}'")" = "1" ]; then
    continue
  fi
  echo "[migrate] Aplicando ${name}..."
  # BEGIN + migración + registro + COMMIT en una sola sesión: si la migración
  # falla, ON_ERROR_STOP aborta la transacción y no se registra nada.
  { echo "BEGIN;"; cat "${path}"; \
    printf "INSERT INTO public.schema_migrations (filename) VALUES ('%s');\n" "${name}"; \
    echo "COMMIT;"; } | psql_exec
  applied=$((applied + 1))
done

echo "[migrate] OK. Migraciones nuevas aplicadas: ${applied}"
