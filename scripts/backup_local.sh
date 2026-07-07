#!/bin/bash
# Genera un backup (esquema + datos) de TU Supabase LOCAL en ./backups, para que sea
# el dump que reciben los devs nuevos vía load_local.sh. A diferencia de
# backup_supabase.sh (que vuelca prod), este vuelca lo que ya tenés corriendo en
# local -- útil cuando local está más al día que prod (ej. migraciones nuevas que
# aún no subieron), para que un dev que clona hoy arranque con tu mismo estado
# exacto en vez de uno viejo de prod.
#
# Requiere Supabase local corriendo: `npx supabase start`.
# Uso:  ./scripts/backup_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="54322"
USER="postgres"
PASSWORD="postgres"
DB="postgres"

if ! docker ps --format '{{.Names}}' | grep -qE "supabase_db_"; then
  echo "ERROR: Supabase local no está corriendo. Ejecuta: npx supabase start" >&2
  exit 1
fi

STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p backups

echo "[backup] Volcando Supabase local (puerto ${PORT}, formato custom)..."
docker run --rm -e PGPASSWORD="${PASSWORD}" postgres:17 \
  pg_dump -h host.docker.internal -p "${PORT}" -U "${USER}" -d "${DB}" \
  --schema=public --no-owner --no-privileges -Fc \
  > "backups/local_${STAMP}.dump"

echo "[backup] Listo: backups/local_${STAMP}.dump"
