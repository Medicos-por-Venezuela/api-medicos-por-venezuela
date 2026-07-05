#!/usr/bin/env bash
# =====================================================================
# Levanta el BACKEND en local para testing del frontend. (Mac / Linux / Git-Bash)
#   Uso:   ./dev.sh            levanta y migra
#          ./dev.sh down       apaga todo
#
# NO requiere Python: las migraciones corren DENTRO del contenedor.
# Único requisito: Docker Desktop corriendo.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "down" ]; then
  docker compose down
  echo "Backend apagado."
  exit 0
fi

command -v docker >/dev/null 2>&1 || { echo "ERROR: falta Docker. Instalá Docker Desktop y abrilo." >&2; exit 1; }

echo "==> Levantando db + api (docker compose)..."
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
echo "   Swagger:  http://localhost:8000/docs"
echo "   Health:   http://localhost:8000/api/v1/health"
echo "   Apagar:   ./dev.sh down"
