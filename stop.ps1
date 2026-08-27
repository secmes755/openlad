# =============================================================================
# OpenLAD External Mode stop script (Windows PowerShell)
#
# Stops the OpenLAD API service (does NOT manage llama-server processes).
# To stop LLM / Embedding backends, handle them separately.
#
# Usage:
#   .\stop.ps1                 # stop whatever listens on $env:OPENLAD_PORT or 11296
#   $env:OPENLAD_PORT = "11300"; .\stop.ps1
# =============================================================================
$ErrorActionPreference = "SilentlyContinue"

if (Get-Item "Env:OPENLAD_PORT" -ErrorAction SilentlyContinue) {
    $Port = [int]$env:OPENLAD_PORT
} else {
    $Port = 11296
}

Write-Host "Stopping OpenLAD (port $Port)..."

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "No OpenLAD process found (port $Port not listening)."
    exit 0
}

$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    Write-Host "Found process PID=$procId ($($proc.ProcessName)) on port $Port, stopping..."
    # Graceful first: CTRL_BREAK-style close so uvicorn can finish in-flight work.
    Stop-Process -Id $procId -ErrorAction SilentlyContinue

    $stopped = $false
    foreach ($i in 1..10) {
        Start-Sleep -Seconds 1
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            $stopped = $true
            break
        }
    }
    if (-not $stopped) {
        Write-Host "Process not responding, force-killing..."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Warning: port $Port is still listening." -ForegroundColor Yellow
    exit 1
}
Write-Host "OpenLAD stopped."
