# AgentFlow — Windows launcher
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
#   c) C:\waldiez\agentflow  (default install location)
#   d) nowhere yet → clone it there

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$cwd       = (Get-Location).Path
$default   = "C:\waldiez\agentflow"

if (Is-AgentflowRepo $cwd) {
    $repoDir = $cwd
    Write-Host "Using agentflow repo in current folder: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} elseif (Is-AgentflowRepo $scriptDir) {
    $repoDir = $scriptDir
    Write-Host "Using agentflow repo at: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} elseif (Is-AgentflowRepo $default) {
    $repoDir = $default
    Write-Host "Found agentflow at: $repoDir"
    Set-Location $repoDir
    git fetch
    git pull
} else {
    Write-Host "AgentFlow not found. Cloning into C:\waldiez\ ..."
    $base = "C:\waldiez"
    if (-not (Test-Path $base)) {
        New-Item -ItemType Directory -Path $base | Out-Null
    }
    Set-Location $base
    git clone https://github.com/waldiez/agentflow
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

# ── 3. Free up ports used by AgentFlow ───────────────────────────────────────
#
# Ports we need: 1883 (MQTT TCP), 9001 (MQTT WS)
# Stop any running container that is occupying these ports — but leave
# everything else alone.

$neededPorts = @("1883", "9001")

$running = docker ps --format "{{.ID}} {{.Names}} {{.Ports}}" 2>$null
if ($running) {
    foreach ($line in $running) {
        $parts = $line -split " ", 3
        $id    = $parts[0]
        $name  = $parts[1]
        $ports = if ($parts.Count -ge 3) { $parts[2] } else { "" }

        # Skip our own mosquitto container
        if ($name -eq "mosquitto") { continue }

        foreach ($port in $neededPorts) {
            if ($ports -match "0\.0\.0\.0:$port->|:::$port->") {
                Write-Host "Port $port is used by container '$name' — stopping it..."
                docker stop $id | Out-Null
                break
            }
        }
    }
}

# ── 4. Mosquitto (MQTT broker) ────────────────────────────────────────────────

$confPath = Join-Path $repoDir "infra\mosquitto\mosquitto.conf"

try {
    $status = docker inspect --format "{{.State.Status}}" mosquitto 2>$null
    if ($LASTEXITCODE -ne 0) { $status = "missing" }
} catch {
    $status = "missing"
}

switch ($status) {
    "running" {
        Write-Host "mosquitto is already running."
    }
    "missing" {
        Write-Host "Creating and starting mosquitto container..."
        docker run -d --name mosquitto `
            --restart unless-stopped `
            -p 1883:1883 `
            -p 9001:9001 `
            -v "${confPath}:/mosquitto/config/mosquitto.conf" `
            eclipse-mosquitto:2.0
    }
    default {
        Write-Host "Starting existing mosquitto container (was: $status)..."
        docker start mosquitto
    }
}

# ── 5. Install AgentFlow ──────────────────────────────────────────────────────

Write-Host ""
Write-Host "Installing / updating AgentFlow..."
pip install --force-reinstall -e ".[all]"

# ── 6. Launch ─────────────────────────────────────────────────────────────────

Write-Banner "AgentFlow is starting — open http://localhost:8080 in your browser"

agentflow
