# =============================================================================
# OpenLAD External Mode startup script (Windows PowerShell)
#
# This script only starts the OpenLAD API service and does NOT manage
# llama-server processes. You must deploy LLM / Embedding backends
# (llama-server / vLLM / Ollama) separately, and point to them via
# OPENLAD_LLM_URL / OPENLAD_EMB_URL.
#
# Usage:
#   .\start.ps1                          # foreground, Ctrl+C to stop
#   $env:OPENLAD_PORT = "11300"; .\start.ps1   # with overrides
#
# All settings can also be persisted in a .env file at the repo root.
# =============================================================================
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Set-EnvDefault([string]$Name, [string]$Value) {
    if (-not (Get-Item "Env:$Name" -ErrorAction SilentlyContinue)) {
        Set-Item "Env:$Name" $Value
    }
}

# -- Environment variables (can be overridden in .env or the session) --
# Model service URLs (default to common ports)
Set-EnvDefault "OPENLAD_LLM_URL" "http://127.0.0.1:8080/v1"
Set-EnvDefault "OPENLAD_EMB_URL" "http://127.0.0.1:8081/v1"
if (-not (Get-Item "Env:OPENLAD_CHART_VLM_URL" -ErrorAction SilentlyContinue)) {
    Set-Item "Env:OPENLAD_CHART_VLM_URL" $env:OPENLAD_LLM_URL
}

# Model names (matching backend registered model names)
Set-EnvDefault "OPENLAD_LLM_MODEL" ""
Set-EnvDefault "OPENLAD_EMB_MODEL" ""
if (-not (Get-Item "Env:OPENLAD_CHART_VLM_MODEL" -ErrorAction SilentlyContinue)) {
    Set-Item "Env:OPENLAD_CHART_VLM_MODEL" $env:OPENLAD_LLM_MODEL
}

Set-EnvDefault "OPENLAD_LLM_MMPROJ_PATH" ""

# API service
Set-EnvDefault "OPENLAD_HOST" "0.0.0.0"
Set-EnvDefault "OPENLAD_PORT" "11296"
if (-not (Get-Item "Env:OPENLAD_API_HOST" -ErrorAction SilentlyContinue)) {
    Set-Item "Env:OPENLAD_API_HOST" $env:OPENLAD_HOST
}
if (-not (Get-Item "Env:OPENLAD_API_PORT" -ErrorAction SilentlyContinue)) {
    Set-Item "Env:OPENLAD_API_PORT" $env:OPENLAD_PORT
}

# Data directory (repo-local by default; gitignored)
Set-EnvDefault "OPENLAD_DATA_DIR" (Join-Path $ScriptDir "data")

# Concurrency policy (External mode defaults to serial)
Set-EnvDefault "OPENLAD_QUERY_CONCURRENCY_MODE" "serial"
Set-EnvDefault "OPENLAD_QUERY_MAX_CONCURRENT" "1"

Write-Host "==============================================" 
Write-Host " OpenLAD (External Mode, Windows)"
Write-Host "==============================================" 
Write-Host " LLM backend:   $env:OPENLAD_LLM_URL"
Write-Host " Embedding:     $env:OPENLAD_EMB_URL"
Write-Host " API listening: http://$($env:OPENLAD_API_HOST):$($env:OPENLAD_API_PORT)"
Write-Host " Data dir:      $env:OPENLAD_DATA_DIR"
Write-Host " Concurrency:   $env:OPENLAD_QUERY_CONCURRENCY_MODE (max $env:OPENLAD_QUERY_MAX_CONCURRENT)"
Write-Host "==============================================" 
Write-Host ""
Write-Host "[!] Make sure LLM / Embedding services are running," -ForegroundColor Yellow
Write-Host "    otherwise OpenLAD will start but queries will fail." -ForegroundColor Yellow
Write-Host ""

# Detect Python interpreter: prefer repo-local venv, fall back to system python
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonBin = $VenvPython
    Write-Host "Using venv Python: $PythonBin"
} else {
    $PythonBin = "python"
    Write-Host "Using system Python: $((Get-Command python).Source)"
}
Write-Host ""

# Start API (foreground)
& $PythonBin -m uvicorn core.api.main:app `
    --host $env:OPENLAD_API_HOST `
    --port $env:OPENLAD_API_PORT `
    --log-level info
