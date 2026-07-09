# =====================================================================
# Levanta BACKEND + Supabase local para testing del frontend. (Windows PowerShell)
#   Uso:   .\dev.ps1            levanta Supabase local + api y migra
#          .\dev.ps1 down       apaga la api (Supabase local sigue corriendo aparte)
#
# NO requiere Python: las migraciones corren DENTRO del contenedor.
# Requisitos: Docker Desktop corriendo + Node (para `npx`). Ver README ->
# "Supabase local" para el detalle de qué hace cada paso.
# Si PowerShell bloquea el script:  powershell -ExecutionPolicy Bypass -File .\dev.ps1
# =====================================================================
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($args[0] -eq "down") {
  docker compose down
  Write-Host "Backend apagado. (Supabase local sigue corriendo; 'npx supabase stop' para apagarlo)"
  exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Error "Falta Docker. Instalá Docker Desktop y abrilo."
  exit 1
}
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
  Write-Error "Falta Node.js (necesario para 'npx supabase')."
  exit 1
}

Write-Host "==> Verificando Supabase local..."
npx supabase status *> $null
if ($LASTEXITCODE -ne 0) { npx supabase start }

# Primera vez (BD recien creada, sin schema_migrations todavia): si hay un dump de prod
# en .\backups y credenciales en .env.supabase, restaura datos REALES antes de migrar.
# En corridas siguientes no toca nada (nunca pisa datos ya cargados).
# El contenedor se resuelve por patron (como en scripts/load_local.sh): el nombre incluye
# el directorio del proyecto, y hardcodearlo fallaba en silencio si la carpeta se renombra.
$dbContainer = docker ps --format '{{.Names}}' | Where-Object { $_ -like 'supabase_db_*' } | Select-Object -First 1
$hasSchema = if ($dbContainer) {
  docker exec $dbContainer psql -U postgres -d postgres `
    -tAc "select to_regclass('public.schema_migrations') is not null" 2>$null
} else { $null }
if ($hasSchema -ne "t") {
  $dump = Get-ChildItem backups\*.dump -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($dump -and (Test-Path ".env.supabase") -and (Get-Command bash -ErrorAction SilentlyContinue)) {
    Write-Host "==> Primera vez: restaurando datos reales de prod ($($dump.Name))..."
    bash ./scripts/load_local.sh
  }
}

Write-Host "==> Levantando la API (docker compose)..."
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
Write-Host "   Swagger:      http://localhost:8000/docs"
Write-Host "   Health:       http://localhost:8000/api/v1/health"
Write-Host "   Supabase API: http://localhost:54321   -   Studio: http://localhost:54323"
Write-Host "   Apagar api:   .\dev.ps1 down   -   Apagar Supabase: npx supabase stop"
