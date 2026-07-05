#!/bin/bash
# Restaura el backup más reciente de ./backups en el Postgres de Supabase LOCAL
# (`npx supabase start`; NO un Postgres propio). Útil para tener datos reales
# (PII) al testear en local. Solo `--schema=public` (ver backup_supabase.sh):
# no toca los esquemas internos de Supabase (auth/storage/realtime).
#
# Requiere Supabase local corriendo: `npx supabase start`.
# Uso:  ./scripts/load_local.sh
set -euo pipefail

cd "$(dirname "$0")/.."

DB="postgres"
USER="postgres"
PASSWORD="postgres"
PORT="54322"

DUMP=$(ls -1t backups/*.dump 2>/dev/null | head -n 1 || true)
if [ -z "${DUMP}" ]; then
  echo "ERROR: no hay backups en ./backups (*.dump). Genera uno con scripts/backup_supabase.sh" >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qE "supabase_db_"; then
  echo "ERROR: Supabase local no está corriendo. Ejecuta: npx supabase start" >&2
  exit 1
fi

echo "[load] Restaurando ${DUMP} en Supabase local (puerto ${PORT})..."
# pg_restore desde un contenedor postgres:17 desechable (mismo patrón que
# backup_supabase.sh), conectando a host.docker.internal (el Postgres de
# Supabase local publica el puerto en el HOST, no en la red de este contenedor).
# --disable-triggers: evita que FKs a auth.users (vacías fuera de este dump)
# bloqueen la carga; el dump es solo del esquema public.
docker run --rm -i -e PGPASSWORD="${PASSWORD}" postgres:17 \
  pg_restore -h host.docker.internal -p "${PORT}" -U "${USER}" -d "${DB}" \
  --no-owner --no-privileges --disable-triggers < "${DUMP}" || true

echo "[load] Aplicando migraciones pendientes con el CLI..."
# Python del venv (Unix o Windows/Git-Bash); si no, el python del PATH.
PY=".venv/bin/python"
[ -x "${PY}" ] || PY=".venv/Scripts/python.exe"
[ -x "${PY}" ] || PY="python"
POSTGRES_HOST=localhost POSTGRES_PORT="${PORT}" POSTGRES_USER="${USER}" \
  POSTGRES_PASSWORD="${PASSWORD}" POSTGRES_DB="${DB}" POSTGRES_SSLMODE=disable \
  "${PY}" scripts/migrate.py up

echo "[load] Listo."
