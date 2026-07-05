# =====================================================================
# Levanta el BACKEND en local para testing del frontend. (Windows PowerShell)
#   Uso:   .\dev.ps1            levanta y migra
#          .\dev.ps1 down       apaga todo
#
# NO requiere Python: las migraciones corren DENTRO del contenedor.
# Único requisito: Docker Desktop corriendo.
# Si PowerShell bloquea el script:  powershell -ExecutionPolicy Bypass -File .\dev.ps1
# =====================================================================
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($args[0] -eq "down") {
  docker compose down
  Write-Host "Backend apagado."
  exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Error "Falta Docker. Instalá Docker Desktop y abrilo."
  exit 1
}

Write-Host "==> Levantando db + api (docker compose)..."
docker compose up -d --build

Write-Host "==> Esperando a la API..."
for ($i = 0; $i -lt 30; $i++) {
  try { Invoke-WebRequest -UseBasicParsing http://localhost:8000/api/v1/health -TimeoutSec 2 | Out-Null; break }
  catch { Start-Sleep -Seconds 2 }
}

Write-Host "==> Aplicando migraciones (dentro del contenedor, sin Python en tu maquina)..."
docker compose exec -T api python artisan migrate

Write-Host ""
Write-Host "OK Backend listo:  http://localhost:8000"
Write-Host "   Swagger:  http://localhost:8000/docs"
Write-Host "   Health:   http://localhost:8000/api/v1/health"
Write-Host "   Apagar:   .\dev.ps1 down"
