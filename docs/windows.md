# Wactorz on Windows

Covers x86-64 (Intel/AMD) and **ARM64** (Snapdragon X, Surface Pro X, Copilot+ PCs).

---

## Which option fits you?

| Option | Docker needed | Node.js needed | ARM64 native | Best for |
|---|---|---|---|---|
| **A — Full Docker** | ✅ | ✗ | ✅ via emulation | Fastest start, production-like |
| **B — Dev mode** | ✅ | ✅ | ✅ | Frontend development |

**Recommended starting point**: Option A for a working dashboard in under 5 minutes.

---

## Prerequisites

### Install Windows Terminal (highly recommended)

```powershell
winget install Microsoft.WindowsTerminal
```

### Install Git

```powershell
winget install Git.Git
```

Includes **Git Bash** — a minimal Unix shell that can run `.sh` scripts.
After install: open Git Bash from the Start menu or Windows Terminal.

### Install Docker Desktop

```powershell
winget install Docker.DockerDesktop
```

Requires Windows 10 22H2+ or Windows 11.
After install, open Docker Desktop and wait for the engine to start.

**ARM64 note:** Docker Desktop on ARM64 Windows (Snapdragon X, Surface Pro) runs
`linux/arm64` containers natively and `linux/amd64` via QEMU emulation.

### Install Node.js (needed for Option B)

```powershell
winget install OpenJS.NodeJS.LTS
```

ARM64 native builds are available and installed automatically by `winget`.

---

## Option A — Full Docker (simplest)

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz

# Copy the example env and set your LLM key
copy .env.template .env
notepad .env   # set LLM_API_KEY at minimum
```

```powershell
docker compose --profile python-full up -d
```

Open **http://localhost:8888/** — the monitor dashboard. All agents should appear
within a few seconds. (The `python-full` profile also starts Fuseki on `:3030` and
Home Assistant on `:8123`; use `--profile python` for the app + MQTT only.)

To stop:

```powershell
docker compose --profile python-full down
```

---

## Option B — Dev mode (no LLM key)

The mock simulator publishes realistic MQTT events so you can develop the frontend
without a running backend.

```powershell
# Terminal 1 — MQTT broker + mock agents
docker compose -f compose.dev.yaml up -d

# Terminal 2 — Vite dev server (hot-reload)
cd frontend
npm install
npm run dev
```

Open **http://localhost:3000**.

Agents appear immediately (main-actor, monitor-agent, io-agent, qa-agent, nautilus,
weather, news). Chat, heartbeats, alerts, and dynamic spawns are all simulated.

To stop the mock stack:

```powershell
docker compose -f compose.dev.yaml down
```

---

## Running bash scripts on Windows

The `scripts/` directory contains bash (`.sh`) scripts. On Windows, run them via:

### Git Bash (simplest)

```bash
# Open Git Bash terminal, then:
cd /c/Users/<your-name>/wactorz
bash scripts/<script>.sh
```

### WSL2 (best compatibility)

```bash
# Inside WSL2 Ubuntu:
cd /mnt/c/Users/<your-name>/wactorz
bash scripts/<script>.sh
```

---

## SSH keys (NautilusAgent + deploy)

Windows 10/11 includes OpenSSH Client. Generate a deploy key:

```powershell
ssh-keygen -t ed25519 -C "wactorz-deploy" -f "$env:USERPROFILE\.ssh\wactorz_deploy" -N '""'
```

Copy the public key to the remote host:

```powershell
type "$env:USERPROFILE\.ssh\wactorz_deploy.pub" | ssh user@host "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Set in `.env`:

```env
NAUTILUS_SSH_KEY=~/.ssh/wactorz_deploy
```

**NautilusAgent `rsync` command**: `rsync` is not available natively on Windows.
Options:
- Use WSL2 (rsync is installed by default)
- Install `rsync` via Chocolatey: `choco install rsync`
- Use Git for Windows rsync: `C:\Program Files\Git\usr\bin\rsync.exe`

For the last option, ensure Git Bash's `bin` is on `PATH` or point `NAUTILUS_RSYNC_PATH`
to the binary.

---

## Environment variable gotchas

**`.env` file encoding**: save as **UTF-8 without BOM**. Notepad on Windows 11 defaults
to UTF-8; older Notepad may default to ANSI. Use VS Code or Notepad++ if unsure.

**Path separators**: in `.env`, always use forward slashes or escaped backslashes:

```env
# ✅ Works on all platforms
NAUTILUS_SSH_KEY=~/.ssh/wactorz_deploy

# ✅ Also works
NAUTILUS_SSH_KEY=C:/Users/alice/.ssh/wactorz_deploy

# ❌ Will fail — backslash is escape character in some parsers
NAUTILUS_SSH_KEY=C:\Users\alice\.ssh\wactorz_deploy
```

**Line endings**: the `.env` file should use LF (Unix) endings. If you edit with
Notepad, it may add CRLF (`\r\n`) which can break the parser. In VS Code:
click the `CRLF` indicator in the bottom-right status bar → change to `LF`.

---

## Troubleshooting

### `docker: command not found` in Git Bash

Docker Desktop adds itself to PATH for PowerShell and CMD but not always Git Bash.
Run Docker commands in PowerShell, or add Docker to Git Bash's PATH:

```bash
export PATH="$PATH:/c/Program Files/Docker/Docker/resources/bin"
```

### Monitor port 8888 already in use

Change the published port in `.env`:

```env
MONITOR_EXTERNAL_PORT=8889
```

Then open **http://localhost:8889/**.

### `MQTT_HOST` connection refused

Ensure Mosquitto is running:

```powershell
docker compose --profile python ps
```

When running the app inside Docker, `MQTT_HOST` should be `mosquitto`; when running
the Python app directly on the host against a containerised broker, use `localhost`.

### ARM64: Docker image `exec format error`

The pulled image is `linux/amd64`; enable QEMU emulation in Docker Desktop
(Settings → Docker Engine → add `"experimental": true`), or pull/build a native
`linux/arm64` image.

### `rsync: command not found` (NautilusAgent)

Install via Chocolatey:

```powershell
# Install Chocolatey first if needed:
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# Then install rsync:
choco install rsync
```

Or use WSL2 where rsync comes pre-installed.

---

## Recommended setup for ARM64 Windows (Copilot+ / Snapdragon X)

```powershell
# 1. Install prerequisites
winget install Git.Git Microsoft.WindowsTerminal OpenJS.NodeJS.LTS Docker.DockerDesktop

# 2. Enable WSL2 (for bash scripts and rsync)
wsl --install   # installs Ubuntu by default; reboot when prompted

# 3. Clone the repo
git clone https://github.com/waldiez/wactorz
cd wactorz
copy .env.template .env
notepad .env   # set LLM_API_KEY

# 4. Start the mock dev stack
docker compose -f compose.dev.yaml up -d
cd frontend && npm install && npm run dev

# → http://localhost:3000  ✓
```
