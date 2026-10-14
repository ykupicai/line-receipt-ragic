# Restart the LINE receipt bot: stop uvicorn on :8000, re-run start_bot.ps1, health-check.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File restart_bot.ps1
$p = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
     Select-Object -ExpandProperty OwningProcess -Unique
if ($p) { Stop-Process -Id $p -Force }
Start-Sleep -Seconds 2
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_bot.ps1")
Start-Sleep -Seconds 7
try {
    "health: " + (Invoke-RestMethod http://localhost:8000/ -TimeoutSec 10).status
} catch {
    "health FAIL: $_"
}
