# One-shot build: front end, then executable.
#
# The result in dist\ contains no credentials, no contact database and no
# resume. Those are created on the machine that runs it, by the setup console.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "building dashboard..."
Set-Location (Join-Path $root "dashboard")
npm install
npm run build

Set-Location $root
Write-Host "building executable..."
python -m PyInstaller packaging\mailbot.spec --noconfirm --distpath dist --workpath build

Write-Host ""
Write-Host "done -> dist\mailbot.exe"
Write-Host "Check it before sharing:  python -m src.main verify-build"
