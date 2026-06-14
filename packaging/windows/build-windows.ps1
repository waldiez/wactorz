# Build the Windows installer: PyInstaller (onedir) -> Inno Setup (Setup.exe).
#
# Prereqs (on Windows):
#   pip install pyinstaller
#   pip install -e ".[desktop]"            # pulls pywebview + pythonnet (WebView2)
#   Inno Setup installed, with iscc.exe on PATH
#   WebView2 runtime present (default on Win10/11; else see wactorz.iss notes)
#
# Packages whatever is in static/app; run 'make build-frontend' first if the SPA
# changed (needs bun).
#Requires -Version 5
$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$root = (Resolve-Path (Join-Path $here "..\..")).Path
$version = (& python -c "import wactorz._version as v; print(v.__version__)").Trim()

Push-Location $root
try {
    Write-Host "==> [1/2] Freezing with PyInstaller (v$version)"
    pyinstaller --noconfirm --clean `
        --distpath dist --workpath dist\pyi-build `
        packaging\pyinstaller\wactorz-desktop.spec

    Write-Host "==> [2/2] Building installer with Inno Setup"
    iscc /DMyAppVersion=$version packaging\windows\wactorz.iss
}
finally {
    Pop-Location
}
Write-Host "==> Done: dist\Wactorz-Setup-$version.exe"
