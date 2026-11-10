# Start a resident laya-serve process (plan C).
#
# The checkpoint is loaded once in this process, so backend restarts and
# --reload cycles no longer wait ~74s.
# Backend side: SYSTEM_ONE_PROVIDER=laya_http and SYSTEM_ONE_BASE_URL=http://127.0.0.1:8001
#
# Usage (project root): powershell -ExecutionPolicy Bypass -File scripts/start_laya_serve.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$modelDir = Join-Path $root "models\laya\multilingual"
if (-not (Test-Path $modelDir)) {
    throw "local checkpoint not found: $modelDir (download the multilingual weights first)"
}

# Registry name; the backend request body "model" field must match it.
$modelName = "kangban"

# laya-serve defaults to 8000, which collides with the backend. Bind 8001 on loopback only.
$env:LAYA_HOST = "127.0.0.1"
$env:LAYA_PORT = "8001"
$env:LAYA_DEVICE = "cpu"
$env:LAYA_PRELOAD = "1"          # load at startup instead of on first request
$env:LAYA_MODELS = $modelName     # preload this checkpoint only
$env:LAYA_LOG_LEVEL = "info"

# LAYA_EXTRA_MODELS registers the local directory as a checkpoint named "kangban".
$env:LAYA_EXTRA_MODELS = (@{
    $modelName = ($modelDir -replace '\\', '/')
} | ConvertTo-Json -Compress)

# Physical core count; oversubscribing hyperthreads hurts CPU inference.
$cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
if ($cores) { $env:LAYA_THREADS = "$cores" }

# Optional auth: only active when LAYA_API_KEY is already set in the environment.
if ($env:LAYA_API_KEY) {
    Write-Host "Bearer auth enabled (LAYA_API_KEY is set)"
}

Write-Host "laya-serve: http://127.0.0.1:$($env:LAYA_PORT)  model=$modelName  dir=$modelDir"

& (Join-Path $root ".venv\Scripts\laya-serve.exe")
