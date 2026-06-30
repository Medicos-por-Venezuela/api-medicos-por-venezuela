#!/bin/bash
# Restaura el backup más reciente de ./backups en el Postgres local que corre en
# docker-compose (contenedor "mpv-db"). Útil para recargar datos sin recrear el
# volumen. Para una carga limpia desde cero: borra el volumen con
#   docker compose down -v && docker compose up -d
# (la restauración automática de db/init/01-restore-from-backup.sh se encargará).
#
# Uso:  ./scripts/load_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER="mpv-db"
DB="${POSTGRES_DB:-medicos}"
USER="${POSTGRES_USER:-postgres}"

DUMP=$(ls -1t backups/*.dump 2>/dev/null | head -n 1 || true)
if [ -z "${DUMP}" ]; then
  echo "ERROR: no hay backups en ./backups (*.dump). Genera uno con scripts/backup_supabase.sh" >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "ERROR: el contenedor ${CONTAINER} no está corriendo. Ejecuta: docker compose up -d db" >&2
  exit 1
fi

echo "[load] Restaurando ${DUMP} en ${CONTAINER}/${DB}..."
docker exec -i "${CONTAINER}" pg_restore \
  --no-owner --no-privileges --disable-triggers \
  -U "${USER}" -d "${DB}" < "${DUMP}" || true

echo "[load] Listo."
