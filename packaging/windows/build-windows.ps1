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
# Read the version from the file — importing the wactorz package pulls in the
# whole backend stack (psutil, …), which the build host's python need not have.
$version = ([regex]::Match((Get-Content -Raw "$root\wactorz\_version.py"), '__version__\s*=\s*"([^"]+)"')).Groups[1].Value

Push-Location $root
try {
    # Freeze from a dedicated, isolated venv (in gitignored .local/) so the
    # bundle is reproducible and not polluted by the dev env. Delete
    # .local\build-venv to refresh its deps.
    $venv    = Join-Path $root ".local\build-venv"
    $venvBin = Join-Path $venv "Scripts"
    if (-not (Test-Path (Join-Path $venvBin "pyinstaller.exe"))) {
        Write-Host "==> [0/2] Creating build venv: $venv"
        New-Item -ItemType Directory -Force -Path (Join-Path $root ".local") | Out-Null
        py -m venv $venv
        & (Join-Path $venvBin "python.exe") -m pip install --upgrade pip pyinstaller
        # [all] = LLM providers + integrations + desktop, minus heavy ml/torch.
        # On Windows PySide6 is excluded (Linux-only) -> WebView2, not Qt.
        $env:WACTORZ_FRONTEND_STALE = "99999999"
        & (Join-Path $venvBin "python.exe") -m pip install -e ".[all]"
    }

    Write-Host "==> [1/2] Freezing with PyInstaller (v$version)"
    & (Join-Path $venvBin "pyinstaller.exe") --noconfirm --clean `
        --distpath dist --workpath dist\pyi-build `
        packaging\pyinstaller\wactorz-desktop.spec

    Write-Host "==> [2/2] Building installer with Inno Setup"
    iscc /DMyAppVersion=$version packaging\windows\wactorz.iss
}
finally {
    Pop-Location
}
Write-Host "==> Done: dist\Wactorz-Setup-$version.exe"
