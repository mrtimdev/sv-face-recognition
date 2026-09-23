# Build SV Face ID for Windows.
#
# Usage (PowerShell):
#   .\scripts\build.ps1
#
# Prerequisites:
#   - Python 3.9+ with venv
#   - OpenCV, NumPy and PyQt6 wheels (installed from requirements.txt)
#   - CMake
#
# Output: dist\SV Face ID\  (folder with SV Face ID.exe)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot)

Write-Host "==> Activating venv..." -ForegroundColor Cyan
& .\venv\Scripts\Activate.ps1

Write-Host "==> Installing PyInstaller..." -ForegroundColor Cyan
pip install pyinstaller --quiet

Write-Host "==> Cleaning previous build..." -ForegroundColor Cyan
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }

Write-Host "==> Building..." -ForegroundColor Cyan
pyinstaller build.spec --noconfirm

Write-Host ""
Write-Host "==> Done!" -ForegroundColor Green

if (Test-Path "dist\SV Face ID\SV Face ID.exe") {
    Write-Host "    Executable: dist\SV Face ID\SV Face ID.exe" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    To create an installer, use Inno Setup or NSIS:"
    Write-Host "      iscc scripts\installer.iss"
} else {
    Write-Host "    Build output: dist\" -ForegroundColor Yellow
}
