# Run Mesio locally for manual testing — Windows PowerShell.
#
#   powershell -ExecutionPolicy Bypass -File scripts\dev\run_local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev\run_local.ps1 -Phone
#
# Uses its own database, mesio_dev (never mesio_test: the test suite writes
# and deletes rows there). Creates it on the first run, applies every
# migration, then starts the app at http://localhost:8000 with auto-reload.
# The app connects as mesio_app, like production, so RLS is enforced;
# migrations run as postgres.
#
# -Phone listens on the local network so a real phone on the same Wi-Fi can
# scan the QR codes. Open the printed http://<your-PC-IP>:8000 address on the
# computer too: a QR only works when it points at that address, never at
# localhost. Windows may ask to allow Python through the firewall.
#
# Local-only values below (ADMIN_KEY etc.) are NOT the production ones.

param([switch]$Phone)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

$Psql   = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
$Python = ".venv\Scripts\python.exe"
$Db     = "mesio_dev"

$env:PGPASSWORD = "mesio_local_dev"
$exists = & $Psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname = '$Db'"
if ($exists -ne "1") {
    Write-Host "Creating database $Db ..."
    & $Psql -U postgres -h localhost -c "CREATE DATABASE $Db" | Out-Null
    & $Psql -U postgres -h localhost -c "ALTER DATABASE $Db SET timezone TO 'UTC'" | Out-Null
}
Remove-Item Env:PGPASSWORD

$env:DATABASE_URL_ADMIN = "postgresql://postgres:mesio_local_dev@localhost:5432/$Db"
$env:DATABASE_URL       = "postgresql://mesio_app:mesio_app_pw@localhost:5432/$Db"
$env:ADMIN_KEY          = "dev-admin-local"   # superadmin login at /internal/superadmin
$env:EMAIL_BACKEND      = "console"           # emails are printed here, never sent

Write-Host "Applying migrations ..."
& $Python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed" }

$BindHost = "127.0.0.1"
$Base = "http://localhost:8000"
if ($Phone) {
    $BindHost = "0.0.0.0"
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
           Select-Object -First 1).IPAddress
    $Base = "http://$($ip):8000"
}

Write-Host ""
Write-Host "Mesio local:   $Base"
Write-Host "Demo en vivo:  $Base/demo"
Write-Host "Superadmin:    $Base/internal/superadmin  (clave: $($env:ADMIN_KEY))"
Write-Host "Stop with Ctrl+C."
Write-Host ""
# The staff app and the diner chat hold SSE streams open forever; without a
# graceful-shutdown limit a code change makes --reload wait for them and the
# server stops answering until those tabs are closed.
& $Python -m uvicorn app.main:app --reload --timeout-graceful-shutdown 2 --host $BindHost --port 8000
