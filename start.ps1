# Media Deduplicator - Windows Launcher
# Installs dependencies and starts the web server

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Web   = Join-Path $Root "web"
$Req   = Join-Path $Root "requirements.txt"

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "  Media Deduplicator  -  Sonarr/Radarr Companion" -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""

# Locate Python - check PATH first, then common install locations
$python = $null
$candidates = @(
    "python",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe",
    "C:\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
foreach ($c in $candidates) {
    try {
        $v = & $c --version 2>&1
        if ($LASTEXITCODE -eq 0) { $python = $c; break }
    } catch {}
}
if (-not $python) {
    Write-Host "Python not found. Please install Python 3.9+ from https://python.org" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
$pyVer = & $python --version 2>&1
Write-Host "Python: $pyVer  ($python)" -ForegroundColor Green

# Install / upgrade dependencies
Write-Host ""
Write-Host "Installing dependencies..." -ForegroundColor Yellow
& $python -m pip install -q -r $Req
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dependency install failed. Check your pip/network." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "Dependencies ready" -ForegroundColor Green

# Start server
Write-Host ""
Write-Host "Starting web server at http://localhost:8181" -ForegroundColor Cyan
Write-Host "(Close this window to stop the server)" -ForegroundColor Gray
Write-Host ""

Set-Location $Web
$env:PYTHONUTF8 = "1"
& $python app.py