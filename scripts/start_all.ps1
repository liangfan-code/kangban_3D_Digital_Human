# Start laya-serve (8001) and the backend (8000) in order.
#
# 1. laya-serve is launched in its own window and preloads the checkpoint.
# 2. This script waits for GET /health on 8001 before starting uvicorn.
# 3. If laya-serve never becomes ready, the backend still starts: the classifier
#    degrades to the local keyword implementation (visible as system_one_fallback).
#
# Usage (project root): powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "venv python not found: $python"
}

function Test-PortInUse([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue)
}

# ---- 1. laya-serve on 8001 ----
if (Test-PortInUse 8001) {
    Write-Host "[1/2] laya-serve already listening on 8001"
} else {
    Write-Host "[1/2] starting laya-serve ..."
    Start-Process powershell -ArgumentList @(
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $PSScriptRoot "start_laya_serve.ps1")
    ) -WindowStyle Minimized
}

$ready = $false
$deadline = (Get-Date).AddSeconds(240)
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 3 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Seconds 3
    }
}

if ($ready) {
    Write-Host "[1/2] laya-serve ready on http://127.0.0.1:8001"
} else {
    Write-Warning "laya-serve not ready after 240s; backend will fall back to the local classifier"
}

# ---- 2. backend on 8000 ----
if (Test-PortInUse 8000) {
    throw "port 8000 is already in use; stop the existing process first"
}

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1 -ExpandProperty IPAddress)

Write-Host "[2/2] starting backend on http://0.0.0.0:8000"
Write-Host "      local:   http://127.0.0.1:8000/"
if ($lanIp) { Write-Host "      LAN:     http://${lanIp}:8000/" }
Write-Host "      stop laya-serve: Get-NetTCPConnection -LocalPort 8001 | % { Stop-Process -Id `$_.OwningProcess }"

& $python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
