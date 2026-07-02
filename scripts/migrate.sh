#!/bin/bash
# CLI de migraciones (estilo Laravel) con tracking en la tabla schema_migrations.
#
# Comandos:
#   new "<descripción>"          Crea db/migrations/<AAAAMMDD_HHMMSS>_<slug>.sql y muestra la ruta a editar.
#   status  [--local|--remote]   Lista las migraciones: [✓ aplicada] / [· pendiente].
#   up      [--local|--remote]   Aplica las pendientes, en orden, cada una en transacción (default).
#
# Sin comando equivale a `up`. Los comandos que tocan la base (status/up) aceptan el modo de conexión:
#   (por defecto)  docker exec al contenedor local mpv-db   -> devs, desde el host
#   --local        psql directo                             -> dentro del contenedor (init)
#   --remote       psql "$DATABASE_URL"                      -> producción (Supabase)
#
# Convención de cada migración: idempotente (IF NOT EXISTS / ON CONFLICT) y transaccional
# (sin CREATE INDEX CONCURRENTLY). El runner la envuelve en BEGIN/COMMIT y registra su nombre.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-${ROOT}/db/migrations}"
DB_CONTAINER="${DB_CONTAINER:-mpv-db}"
PGUSER="${POSTGRES_USER:-postgres}"
PGDATABASE="${POSTGRES_DB:-medicos}"

# Define psql_exec según el modo de conexión pedido.
resolve_psql() {
  case "${1:-docker}" in
    ""|docker) psql_exec() { docker exec -i "${DB_CONTAINER}" psql -U "${PGUSER}" -d "${PGDATABASE}" -v ON_ERROR_STOP=1 "$@"; } ;;
    --local)   psql_exec() { psql -U "${PGUSER}" -d "${PGDATABASE}" -v ON_ERROR_STOP=1 "$@"; } ;;
    --remote)  [ -n "${DATABASE_URL:-}" ] || { echo "[migrate] --remote requiere la variable DATABASE_URL" >&2; exit 1; }
               psql_exec() { psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 "$@"; } ;;
    *) echo "[migrate] modo desconocido: ${1} (usa: --local | --remote)" >&2; exit 1 ;;
  esac
}

ensure_table() {
  psql_exec -q -c "SET client_min_messages TO warning;
  CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
  );"
}

cmd_new() {
  [ -n "${1:-}" ] || { echo "Uso: scripts/migrate.sh new \"<descripción>\"" >&2; exit 1; }
  # slug: minúsculas, todo lo no alfanumérico -> "_", colapsado, sin "_" al inicio/fin.
  local slug ts file
  slug="$(printf '%s' "$*" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '_' | sed 's/^_//; s/_$//')"
  ts="$(date +%Y%m%d_%H%M%S)"
  file="${MIGRATIONS_DIR}/${ts}_${slug}.sql"
  mkdir -p "${MIGRATIONS_DIR}"
  cat > "${file}" <<EOF
-- Migración: $*
-- Creada:    $(date '+%Y-%m-%d %H:%M:%S')
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT) y transaccional
-- (sin CREATE INDEX CONCURRENTLY). El runner la envuelve en BEGIN/COMMIT.

EOF
  echo "[migrate] Migración creada. Edita este archivo:"
  echo "  ${file#"${ROOT}/"}"
}

cmd_status() {
  resolve_psql "${1:-}"
  ensure_table
  local applied name pending=0 done=0
  applied="$(psql_exec -tAc "SELECT filename FROM public.schema_migrations")"
  echo "Migraciones (${MIGRATIONS_DIR#"${ROOT}/"}):"
  shopt -s nullglob
  for path in "${MIGRATIONS_DIR}"/*.sql; do
    name="$(basename "${path}")"
    if printf '%s\n' "${applied}" | grep -qxF "${name}"; then
      echo "  [✓ aplicada]  ${name}"; done=$((done + 1))
    else
      echo "  [· pendiente] ${name}"; pending=$((pending + 1))
    fi
  done
  [ $((done + pending)) -gt 0 ] || echo "  (no hay migraciones en db/migrations/)"
  echo "Total: ${done} aplicadas, ${pending} pendientes."
}

cmd_up() {
  resolve_psql "${1:-}"
  ensure_table
  local name applied=0
  shopt -s nullglob
  for path in "${MIGRATIONS_DIR}"/*.sql; do
    name="$(basename "${path}")"
    if [ "$(psql_exec -tAc "SELECT 1 FROM public.schema_migrations WHERE filename = '${name}'")" = "1" ]; then
      continue
    fi
    echo "[migrate] Aplicando ${name}..."
    { echo "BEGIN;"; cat "${path}"; \
      printf "INSERT INTO public.schema_migrations (filename) VALUES ('%s');\n" "${name}"; \
      echo "COMMIT;"; } | psql_exec
    applied=$((applied + 1))
  done
  echo "[migrate] OK. Migraciones nuevas aplicadas: ${applied}"
}

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  new)            shift; cmd_new "$@" ;;
  status)         shift; cmd_status "$@" ;;
  up)             shift; cmd_up "$@" ;;
  help|-h|--help) usage ;;
  *)              cmd_up "$@" ;;   # sin comando (o con --local/--remote) -> up
esac
