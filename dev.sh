#!/usr/bin/env bash
# =====================================================================
# Levanta BACKEND + Supabase local para testing del frontend. (Mac / Linux / Git-Bash)
#   Uso:   ./dev.sh            levanta Supabase local + api y migra
#          ./dev.sh down       apaga la api (Supabase local sigue corriendo aparte)
#
# NO requiere Python: las migraciones corren DENTRO del contenedor.
# Requisitos: Docker Desktop corriendo + Node (para `npx`). Ver README ->
# "Supabase local" para el detalle de qué hace cada paso.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "down" ]; then
  docker compose down
  echo "Backend apagado. (Supabase local sigue corriendo; 'npx supabase stop' para apagarlo)"
  exit 0
fi

command -v docker >/dev/null 2>&1 || { echo "ERROR: falta Docker. Instalá Docker Desktop y abrilo." >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "ERROR: falta Node.js (necesario para 'npx supabase')." >&2; exit 1; }

echo "==> Verificando Supabase local..."
npx supabase status >/dev/null 2>&1 || npx supabase start

# Primera vez (BD recién creada, sin schema_migrations todavía): si hay un dump de prod
# en ./backups y credenciales en .env.supabase, restaura datos REALES antes de migrar.
# En corridas siguientes no toca nada (nunca pisa datos ya cargados).
HAS_SCHEMA=$(docker exec supabase_db_api-medicos-por-venezuela psql -U postgres -d postgres \
  -tAc "select to_regclass('public.schema_migrations') is not null" 2>/dev/null || echo "f")
if [ "${HAS_SCHEMA}" != "t" ]; then
  DUMP=$(ls -1t backups/*.dump 2>/dev/null | head -n 1 || true)
  if [ -n "${DUMP}" ] && [ -f ".env.supabase" ]; then
    echo "==> Primera vez: restaurando datos reales de prod (${DUMP})..."
    ./scripts/load_local.sh
  fi
fi

echo "==> Levantando la API (docker compose)..."
docker compose up -d --build

echo "==> Esperando a la API..."
for _ in $(seq 1 30); do
  curl -sf http://localhost:8000/api/v1/health >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Aplicando migraciones (dentro del contenedor, sin Python en tu máquina)..."
docker compose exec -T api python artisan migrate

echo ""
echo "✅ Backend listo:  http://localhost:8000"
echo "   Swagger:      http://localhost:8000/docs"
echo "   Health:       http://localhost:8000/api/v1/health"
echo "   Supabase API: http://localhost:54321   ·   Studio: http://localhost:54323"
echo "   Apagar api:   ./dev.sh down   ·   Apagar Supabase: npx supabase stop"
