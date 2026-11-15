# SPDX-License-Identifier: Apache-2.0
# Wactorz - Windows launcher.
#
# Starts Wactorz from the checkout this script belongs to:
#   1. finds Python 3.11 or later (or reuses the checkout's .venv),
#   2. checks .env, helping set MQTT_PASSWORD and the model provider's key,
#   3. starts the repository's authenticated MQTT broker with Docker Compose,
#   4. installs Wactorz into the checkout's .venv,
#   5. runs Wactorz with that same interpreter.
#
# How to run:
#   double-click scripts\start.bat
#   or: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Update] [-NoPrompt]
#
#   -Update     git pull --ff-only before starting. Without it the checkout is
#               used exactly as it is.
#   -NoPrompt   never open Notepad or ask a question: stop with an error instead.
#
# Any other arguments are passed on to `wactorz`.
#
# Written for Windows PowerShell 5.1 as well as PowerShell 7, and kept to ASCII:
# 5.1 reads a script without a byte-order mark in the ANSI code page.

[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$NoPrompt,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$WactorzArgs
)

$ErrorActionPreference = "Stop"

$MinimumPython = [version]"3.11"

# -- helpers -------------------------------------------------------------------

# Runs a native command whose stderr is redirected. Under Windows PowerShell 5.1
# with ErrorActionPreference=Stop, redirected stderr becomes a terminating
# error, so a failing check would crash instead of reaching its own message.
function Invoke-Quiet([scriptblock]$Command) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $saved }
}

function Write-Banner([string]$Message) {
    Write-Host ""
    Write-Host "=================================================="
    Write-Host "  $Message"
    Write-Host "=================================================="
    Write-Host ""
}

# Stops the launcher with a message saying what to do. The exit code reaches
# start.bat, which passes it on.
function Stop-Launcher([string]$Message, [int]$Code = 1) {
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    Write-Host ""
    exit $Code
}

function Test-Interactive {
    if ($NoPrompt) { return $false }
    if ([Console]::IsInputRedirected) { return $false }
    return [Environment]::UserInteractive
}

# The Python version an interpreter reports, or $null if it does not run.
# `$Prefix` holds arguments that come before ours, such as `-3` for `py`.
function Get-PythonVersion([string]$Exe, [string[]]$Prefix = @()) {
    try {
        $out = Invoke-Quiet { & $Exe @Prefix -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null }
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        return [version]("$out".Trim())
    } catch {
        return $null
    }
}

# The full path of an interpreter, so the virtual environment is made with
# exactly the one that was checked.
function Get-PythonPath([string]$Exe, [string[]]$Prefix = @()) {
    $out = Invoke-Quiet { & $Exe @Prefix -c "import sys; print(sys.executable)" 2>$null }
    return "$out".Trim()
}

# A Python of at least $MinimumPython to make the virtual environment with.
# WACTORZ_PYTHON names one outright. Otherwise the `py` launcher is asked for
# the versions CI tests, newest first, and then for its default; then `python`
# on PATH. Returns @{ Path; Version } or @{ Path = $null; Found = <list> }.
function Find-BasePython {
    $seen = @()
    if ($env:WACTORZ_PYTHON) {
        $v = Get-PythonVersion $env:WACTORZ_PYTHON
        if ($v -and $v -ge $MinimumPython) {
            return @{ Path = (Get-PythonPath $env:WACTORZ_PYTHON); Version = $v }
        }
        if ($v) { $seen += "$v at $env:WACTORZ_PYTHON (WACTORZ_PYTHON)" }
        else { $seen += "nothing that runs at $env:WACTORZ_PYTHON (WACTORZ_PYTHON)" }
        return @{ Path = $null; Found = $seen }
    }
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($tag in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) {
            $candidates += , @("py", @($tag))
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates += , @("python", @())
    }
    foreach ($c in $candidates) {
        $v = Get-PythonVersion $c[0] $c[1]
        if (-not $v) { continue }
        if ($v -ge $MinimumPython) {
            return @{ Path = (Get-PythonPath $c[0] $c[1]); Version = $v }
        }
        $seen += "$v ($($c[0]) $($c[1] -join ' '))".Trim()
    }
    return @{ Path = $null; Found = $seen }
}

# KEY=value lines of a .env file, read the way python-dotenv reads them closely
# enough for the settings checked here: comments and blank lines skipped, an
# `export ` prefix allowed, surrounding quotes removed, and an unquoted value
# cut at ` #`.
function Read-EnvFile([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $m = [regex]::Match($line, '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
        if (-not $m.Success) { continue }
        $value = $m.Groups[2].Value.Trim()
        if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
            $value = $Matches[1]
        } else {
            $value = ($value -replace '\s+#.*$', '').Trim()
        }
        $values[$m.Groups[1].Value] = $value
    }
    return $values
}

# A setting as Wactorz will see it: the process environment wins over .env,
# as python-dotenv does not override variables that are already set.
function Get-Setting([hashtable]$EnvValues, [string]$Name, [string]$Default = "") {
    $fromProcess = [Environment]::GetEnvironmentVariable($Name)
    if ($fromProcess) { return $fromProcess }
    if ($EnvValues.ContainsKey($Name) -and $EnvValues[$Name]) { return $EnvValues[$Name] }
    return $Default
}

# The variables that can hold the key for a provider; any one being set is
# enough. Mirrors wactorz/llm_factory.py. An empty list means no key is needed.
function Get-ProviderKeyNames([string]$Provider, [hashtable]$EnvValues) {
    switch ($Provider.ToLowerInvariant()) {
        "anthropic" { return @("ANTHROPIC_API_KEY", "LLM_API_KEY") }
        "openai" {
            # A local OpenAI-compatible server (OPENAI_URL) may take no key.
            if (Get-Setting $EnvValues "OPENAI_URL") { return @() }
            return @("OPENAI_API_KEY", "LLM_API_KEY")
        }
        "gemini" { return @("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY") }
        "nim" { return @("NIM_API_KEY", "NVIDIA_API_KEY", "LLM_API_KEY") }
        default { return @() }   # ollama, none, fake: no key
    }
}

# Set MQTT_PASSWORD in .env without touching any other line.
function Set-EnvFileValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path) }
    $pattern = '^\s*(?:export\s+)?' + [regex]::Escape($Name) + '\s*='
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Name=$Value"
            $done = $true
            break
        }
    }
    if (-not $done) { $lines += "$Name=$Value" }
    # UTF-8 without a byte-order mark: python-dotenv and Compose both read it.
    [IO.File]::WriteAllLines($Path, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
}

function New-BrokerPassword {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (-join ($bytes | ForEach-Object { $_.ToString("x2") }))
}

# -- 1. the checkout ---------------------------------------------------------------

$repoDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $repoDir "pyproject.toml"))) {
    Stop-Launcher "start.ps1 must stay in the scripts folder of a Wactorz checkout; no pyproject.toml in $repoDir."
}
Set-Location -LiteralPath $repoDir
Write-Host "Wactorz checkout: $repoDir"

if ($Update) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Stop-Launcher "-Update needs Git. Install it with: winget install Git.Git"
    }
    Write-Host "Updating the checkout (git pull --ff-only)..."
    Invoke-Quiet { git -C $repoDir pull --ff-only }
    if ($LASTEXITCODE -ne 0) {
        Stop-Launcher ("git pull --ff-only failed; git's reason is above. Usually the branch has local " +
            "changes, has diverged, or tracks no remote branch. Update the checkout by hand, or run " +
            "the launcher without -Update to start it as it is.")
    }
}

# -- 2. Python ---------------------------------------------------------------------

$venvDir = Join-Path $repoDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (Test-Path -LiteralPath $venvDir) {
    $venvVersion = Get-PythonVersion $venvPython
    if (-not $venvVersion) {
        Stop-Launcher ("$venvDir exists but its Python does not run. It is left as it is. " +
            "To start again, delete it (Remove-Item -Recurse -Force '$venvDir') and run the launcher again.")
    }
    if ($venvVersion -lt $MinimumPython) {
        Stop-Launcher ("$venvDir uses Python $venvVersion, and Wactorz needs $MinimumPython or later. " +
            "It is left as it is. Install a newer Python (winget install Python.Python.3.13), " +
            "delete the environment (Remove-Item -Recurse -Force '$venvDir'), and run the launcher again.")
    }
    Write-Host "Using the existing environment: $venvDir (Python $venvVersion)"
} else {
    $base = Find-BasePython
    if (-not $base.Path) {
        $found = "none"
        if ($base.Found.Count -gt 0) { $found = $base.Found -join "; " }
        Stop-Launcher ("Wactorz needs Python $MinimumPython or later. Found: $found. " +
            "Install one with: winget install Python.Python.3.13, then run the launcher again. " +
            "To use a particular interpreter, set WACTORZ_PYTHON to its python.exe.")
    }
    Write-Host "Using Python $($base.Version): $($base.Path)"
}

# -- 3. .env -----------------------------------------------------------------------

$envFile = Join-Path $repoDir ".env"
$envTemplate = Join-Path $repoDir ".env.template"

if (-not (Test-Path -LiteralPath $envFile)) {
    if (-not (Test-Path -LiteralPath $envTemplate)) {
        Stop-Launcher "No .env and no .env.template in $repoDir."
    }
    Copy-Item -LiteralPath $envTemplate -Destination $envFile
    Write-Host "Created .env from .env.template."
}

$envValues = Read-EnvFile $envFile

# The broker password. Compose refuses to start the broker without one.
if (-not (Get-Setting $envValues "MQTT_PASSWORD")) {
    if (-not (Test-Interactive)) {
        Stop-Launcher ("MQTT_PASSWORD is empty in $envFile. The broker carries agent commands and " +
            "spawned code, so it refuses to start without one. Set it to a long random value and run again.")
    }
    Write-Banner "MQTT_PASSWORD is not set"
    Write-Host "  The MQTT broker needs a password: it carries agent commands and the"
    Write-Host "  code spawned agents run."
    Write-Host ""
    Write-Host "  Press Enter to generate a random one and save it in .env,"
    Write-Host "  or type E to open .env in Notepad and set MQTT_PASSWORD yourself."
    $answer = Read-Host "  [Enter = generate, E = edit]"
    if ($answer -match '^\s*[Ee]') {
        Start-Process notepad -ArgumentList "`"$envFile`"" -Wait
    } else {
        Set-EnvFileValue $envFile "MQTT_PASSWORD" (New-BrokerPassword)
        Write-Host "  Saved a generated MQTT_PASSWORD in .env (not shown)."
    }
    $envValues = Read-EnvFile $envFile
    if (-not (Get-Setting $envValues "MQTT_PASSWORD")) {
        Stop-Launcher "MQTT_PASSWORD is still empty in $envFile. Set it and run the launcher again."
    }
}

# The model provider's key, for the providers that need one.
$provider = Get-Setting $envValues "LLM_PROVIDER" "anthropic"
$keyNames = @(Get-ProviderKeyNames $provider $envValues)
$hasKey = { param($values) @($keyNames | Where-Object { Get-Setting $values $_ }).Count -gt 0 }

if ($keyNames.Count -gt 0 -and -not (& $hasKey $envValues)) {
    $names = $keyNames -join " or "
    if (-not (Test-Interactive)) {
        Stop-Launcher ("LLM_PROVIDER=$provider needs an API key: set $names in $envFile. " +
            "LLM_PROVIDER=ollama runs a local model with no key.")
    }
    Write-Banner "An API key for $provider is needed"
    Write-Host "  .env is opening in Notepad. Set one of: $names"
    Write-Host "  (or change LLM_PROVIDER, e.g. to ollama for a local model with no key)."
    Write-Host "  Save and close Notepad to continue."
    Start-Process notepad -ArgumentList "`"$envFile`"" -Wait
    $envValues = Read-EnvFile $envFile
    $provider = Get-Setting $envValues "LLM_PROVIDER" "anthropic"
    $keyNames = @(Get-ProviderKeyNames $provider $envValues)
    if ($keyNames.Count -gt 0 -and -not (& $hasKey $envValues)) {
        Stop-Launcher "LLM_PROVIDER=$provider still has no key. Set $($keyNames -join ' or ') in $envFile and run again."
    }
}
if ($keyNames.Count -eq 0) {
    Write-Host "Model provider: $provider (no API key needed)."
} else {
    Write-Host "Model provider: $provider (key found)."
}

# -- 4. the MQTT broker --------------------------------------------------------------

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-Launcher "Docker is not installed. Install Docker Desktop (winget install Docker.DockerDesktop), start it, and run the launcher again."
}
Invoke-Quiet { docker info *> $null }
if ($LASTEXITCODE -ne 0) {
    Stop-Launcher "Docker is installed but its engine is not running. Start Docker Desktop, wait until it says the engine is running, and run the launcher again."
}
Invoke-Quiet { docker compose version *> $null }
if ($LASTEXITCODE -ne 0) {
    Stop-Launcher "Docker Compose is not available. Update Docker Desktop, which includes it, and run the launcher again."
}

Write-Host "Starting the MQTT broker (docker compose up -d --wait mosquitto)..."
Invoke-Quiet { docker compose --project-directory $repoDir up -d --wait --wait-timeout 90 mosquitto }
if ($LASTEXITCODE -ne 0) {
    Stop-Launcher "The MQTT broker did not start. See the output above, and 'docker compose logs mosquitto'."
}

# -- 5. the environment --------------------------------------------------------------

if (-not (Test-Path -LiteralPath $venvDir)) {
    Write-Host "Creating the environment: $venvDir"
    Invoke-Quiet { & $base.Path -m venv $venvDir }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Stop-Launcher "Could not create $venvDir with $($base.Path)."
    }
}

# Install when the environment has never had Wactorz installed by this launcher,
# or pyproject.toml has changed since: a rerun on an unchanged checkout starts
# straight away, and an -Update that changes dependencies installs them.
$stamp = Join-Path $venvDir ".wactorz-launcher-installed"
# .NET rather than Get-FileHash: Windows PowerShell started from PowerShell 7
# inherits a module path on which that cmdlet cannot be found.
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([IO.File]::ReadAllBytes((Join-Path $repoDir "pyproject.toml")))
} finally {
    $sha.Dispose()
}
$wanted = -join ($digest | ForEach-Object { $_.ToString("x2") })
$installed = ""
if (Test-Path -LiteralPath $stamp) { $installed = (Get-Content -LiteralPath $stamp -Raw).Trim() }

if ($installed -ne $wanted) {
    Write-Host ""
    Write-Host "Installing Wactorz into $venvDir ..."
    Invoke-Quiet { & $venvPython -m pip install --disable-pip-version-check -e "$repoDir[all]" }
    if ($LASTEXITCODE -ne 0) {
        Stop-Launcher "Installing Wactorz failed. See the pip output above; the environment is left as it is."
    }
    Set-Content -LiteralPath $stamp -Value $wanted -Encoding ASCII
} else {
    Write-Host "Wactorz is already installed in $venvDir."
}

# -- 6. launch -------------------------------------------------------------------------

# The dashboard address, as Wactorz works it out: MONITOR_PORT, then the older
# WS_PORT, then 8888, on WACTORZ_BIND_HOST, which defaults to loopback.
$port = Get-Setting $envValues "MONITOR_PORT"
if (-not $port) { $port = Get-Setting $envValues "WS_PORT" "8888" }
$bindHost = Get-Setting $envValues "WACTORZ_BIND_HOST" "127.0.0.1"
$urlHost = $bindHost
if (@("127.0.0.1", "0.0.0.0", "::", "::1", "localhost") -contains $bindHost) { $urlHost = "localhost" }
if ($urlHost.Contains(":")) { $urlHost = "[$urlHost]" }

Write-Banner "Wactorz is starting - open http://${urlHost}:$port in your browser"

# Called directly rather than through Invoke-Quiet, so the terminal chat keeps
# the console to itself; whatever it writes to stderr is shown, never fatal.
$ErrorActionPreference = "Continue"
& $venvPython -m wactorz @WactorzArgs
exit $LASTEXITCODE
