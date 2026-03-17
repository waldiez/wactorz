# Wactorz — Windows launcher
#
# How to run:
#   Right-click → "Run with PowerShell"
#   — or —
#   powershell -ExecutionPolicy Bypass -File start.ps1

$ErrorActionPreference = "Stop"

# ── helpers ───────────────────────────────────────────────────────────────────

function Is-AgentflowRepo($path) {
    return ($null -ne $path) -and
           (Test-Path (Join-Path $path ".git")) -and
           (Test-Path (Join-Path $path "pyproject.toml"))
}

function Write-Banner($msg) {
    Write-Host ""
    Write-Host "=================================================="
    Write-Host "  $msg"
    Write-Host "=================================================="
    Write-Host ""
}

# ── 1. Figure out where the repo is ──────────────────────────────────────────
#
# Priority:
#   a) current working directory  (user is already inside the repo)
#   b) the folder this script lives in  (script is inside the repo)
#   c) C:\waldiez\wactorz  (default install location)
#   d) nowhere yet → clone it there

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$cwd       = (Get-Location).Path
$default   = "C:\waldiez\wactorz"

if (Is-AgentflowRepo $cwd) {
    $repoDir = $cwd
    Write-Host "Using wactorz repo in current folder: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} elseif (Is-AgentflowRepo $scriptDir) {
    $repoDir = $scriptDir
    Write-Host "Using wactorz repo at: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} elseif (Is-AgentflowRepo $default) {
    $repoDir = $default
    Write-Host "Found wactorz at: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} else {
    Write-Host "Wactorz not found. Cloning into C:\waldiez\ ..."
    $base = "C:\waldiez"
    if (-not (Test-Path $base)) {
        New-Item -ItemType Directory -Path $base | Out-Null
    }
    Set-Location $base
    git clone https://github.com/waldiez/wactorz
    $repoDir = $default
    Set-Location $repoDir
}

# ── 2. Check .env ─────────────────────────────────────────────────────────────

$envFile    = Join-Path $repoDir ".env"
$envExample = Join-Path $repoDir ".env.example"

if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw
    if ($content -notmatch 'LLM_API_KEY\s*=\s*\S+') {
        Write-Banner "WARNING: .env exists but LLM_API_KEY is empty"
        Write-Host "  Opening the file for you. Find the line:"
        Write-Host "      LLM_API_KEY="
        Write-Host "  and paste your Anthropic API key after the = sign."
        Write-Host "  Save and close Notepad, then come back here."
        Write-Host ""
        Start-Process notepad $envFile -Wait
    } else {
        Write-Host ".env is set up correctly."
    }
} elseif (Test-Path $envExample) {
    Copy-Item $envExample $envFile
    Write-Banner ".env created from template — please fill in your API key"
    Write-Host "  The file is opening in Notepad now."
    Write-Host "  Find the line:  LLM_API_KEY="
    Write-Host "  Paste your Anthropic API key after the = sign."
    Write-Host "  Save and close Notepad, then come back here."
    Write-Host ""
    Start-Process notepad $envFile -Wait
} else {
    Write-Host "WARNING: no .env or .env.example found. You may need to create the .env file manually."
}

# ── 3. Mosquitto (MQTT broker) ────────────────────────────────────────────────

Write-Host "Starting mosquitto..."
docker compose up -d

# ── 5. Install Wactorz ──────────────────────────────────────────────────────

Write-Host ""
Write-Host "Installing / updating Wactorz..."
pip install --force-reinstall -e ".[all]"

# ── 6. Launch ─────────────────────────────────────────────────────────────────

Write-Banner "Wactorz is starting — open http://localhost:8080 in your browser"

wactorz
