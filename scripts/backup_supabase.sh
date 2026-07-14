#!/bin/bash
# Genera un backup (esquema + datos) de la base de Supabase en ./backups.
# Usa pg_dump desde un contenedor postgres:17 (la versión debe coincidir con el
# servidor) conectándose por el SESSION pooler (puerto 5432), que sí soporta pg_dump.
#
# Requiere las credenciales de Supabase en .env.supabase (no se versiona).
# Uso:  ./scripts/backup_supabase.sh
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env.supabase"
if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: falta ${ENV_FILE} con las credenciales de Supabase." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "${ENV_FILE}"; set +a

# pg_dump necesita el puerto de sesión (5432), no el transaction pooler (6543).
DUMP_PORT=5432
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p backups

echo "[backup] Volcando esquema + datos (formato custom)..."
docker run --rm -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres:17 \
  pg_dump -h "${POSTGRES_HOST}" -p "${DUMP_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  --schema=public --no-owner --no-privileges -Fc \
  > "backups/supabase_${STAMP}.dump"

echo "[backup] Volcando esquema + datos (SQL plano)..."
docker run --rm -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres:17 \
  pg_dump -h "${POSTGRES_HOST}" -p "${DUMP_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  --schema=public --no-owner --no-privileges \
  > "backups/supabase_${STAMP}.sql"

echo "[backup] Listo:"
ls -la backups/supabase_${STAMP}.*
