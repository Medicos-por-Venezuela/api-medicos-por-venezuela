#!/usr/bin/env bash
# =====================================================================
# Deploy del backend en EC2 (Amazon Linux 2023).
#   Uso:   ./deploy.sh            (rama dev por defecto)
#          ./deploy.sh main       (otra rama)
#          ./deploy.sh dev -y      (sin confirmación de backup)
#
# Orden pensado para expand/contract SIN downtime:
#   1) git pull   2) build imagen nueva   3) migrar desde un contenedor efímero
#   (la app VIEJA sigue sirviendo; tras un rename, la vista de compat la cubre)
#   4) swap de la app a la imagen nueva   5) health check
#
# Notas del entorno: docker sin sudo (usuario en grupo docker); se usa `docker build`
# y no `docker compose --build` porque el buildx del host es < 0.17.
# =====================================================================
set -euo pipefail

COMPOSE="docker-compose.prod.yml"
ENV_FILE=".env.production"
IMAGE="api-medicos-por-venezuela"
HEALTH_URL="http://localhost:8000/api/v1/health"

BRANCH="dev"
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    *) BRANCH="$arg" ;;
  esac
done

cd "$(dirname "$0")"

# --- Pre-check: archivos y backup ---
[ -f "$COMPOSE" ] || { echo "ERROR: no encuentro $COMPOSE (¿estás en el repo?)" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: falta $ENV_FILE en el EC2." >&2; exit 1; }

if [ "$ASSUME_YES" -ne 1 ]; then
  echo "⚠️  Esto aplica migraciones a la Supabase de PRODUCCIÓN."
  read -r -p "¿Tenés un backup reciente? [y/N] " ok
  [ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "Abortado. Hacé el backup y reintentá."; exit 1; }
fi

echo "==> 1/5 git pull ($BRANCH)"
git pull origin "$BRANCH"

echo "==> 2/5 build de la imagen"
docker build -t "$IMAGE" .

echo "==> 3/5 migraciones (contenedor efímero desde la imagen nueva)"
docker compose -f "$COMPOSE" run --rm api python artisan migrate

echo "==> 4/5 swap de la app a la imagen nueva"
docker compose -f "$COMPOSE" up -d

echo "==> 5/5 health check"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
  [ "$code" = "200" ] && break
  sleep 2
done

if [ "${code:-}" = "200" ]; then
  echo "✅ OK — health 200. Deploy completo."
  docker compose -f "$COMPOSE" exec -T api python artisan migrate:status | tail -n 12
else
  echo "❌ ERROR — health devolvió '${code:-sin respuesta}'." >&2
  echo "   Logs: docker compose -f $COMPOSE logs --tail=50 api" >&2
  exit 1
fi
