# Wactorz on Windows

Covers x86-64 (Intel/AMD) and **ARM64** (Snapdragon X, Surface Pro X, Copilot+ PCs).

---

## Which option fits you?

| Option | Docker needed | Rust needed | Node.js needed | ARM64 native | Best for |
|---|---|---|---|---|---|
| **A — Full Docker** | ✅ | ✗ | ✗ | ✅ via emulation | Fastest start, production-like |
| **B — Dev mode** | ✅ | ✗ | ✅ | ✅ | Frontend development |
| **C — Native binary** | partial | ✅ | ✅ | ✅ | Maximum performance, SSH access |

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
The pre-built Wactorz images are `linux/amd64`; they run fine on ARM64 via emulation,
or you can build a native `linux/arm64` image yourself (see below).

### Install Node.js (needed for Options B and C)

```powershell
winget install OpenJS.NodeJS.LTS
```

ARM64 native builds are available and installed automatically by `winget`.

### Install Rust (needed for Option C)

```powershell
winget install Rustlang.Rustup
```

After install, open a new terminal and verify:

```powershell
rustc --version   # should show 1.93+
cargo --version
```

ARM64: `rustup` installs `aarch64-pc-windows-msvc` as the default target automatically.
The MSVC toolchain is used; the Visual Studio Build Tools are installed by `rustup`.

---

## Option A — Full Docker (simplest)

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz

# Copy the example env and set your LLM key
copy .env.example .env
notepad .env   # set LLM_API_KEY at minimum
```

```powershell
docker compose up -d
```

Open **http://localhost/** — all agents should appear within a few seconds.

To stop:

```powershell
docker compose down
```

### ARM64 note for Option A

The default `compose.yaml` pulls `linux/amd64` images and runs them via QEMU.
Performance is acceptable for development. For better performance, build a native image:

```powershell
docker buildx build --platform linux/arm64 --tag wactorz-server:local --load .\rust
```

Then in `compose.yaml`, change:

```yaml
wactorz:
  image: wactorz-server:local   # ← replace the build section with this
  platform: linux/arm64
```

---

## Option B — Dev mode (no Rust build, no LLM key)

The mock simulator publishes realistic MQTT events so you can develop the frontend
without a running Rust backend.

```powershell
# Terminal 1 — MQTT broker + mock agents
docker compose -f compose.dev.yaml up -d

# Terminal 2 — Vite dev server (hot-reload)
cd frontend
npm install
npm run dev
```

Open **http://localhost:3000**.

8 agents appear immediately (main-actor, monitor-agent, io-agent, qa-agent, nautilus,
udx, weather, news). Chat, heartbeats, alerts, and dynamic spawns are all simulated.

To stop the mock stack:

```powershell
docker compose -f compose.dev.yaml down
```

---

## Option C — Native binary on Windows

The Rust binary runs directly on Windows. Only Mosquitto runs in Docker.

### 1. Clone and configure

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz
copy .env.example .env
notepad .env   # set LLM_API_KEY and MQTT_HOST=localhost
```

### 2. Build the frontend

```powershell
cd frontend
npm install
npm run build   # → frontend\dist\
cd ..
```

### 3. Build the Rust binary

```powershell
cd rust
cargo build --release --bin wactorz
cd ..
```

The binary is at `rust\target\release\wactorz.exe`.

**ARM64**: `cargo build` uses the native `aarch64-pc-windows-msvc` target by default —
no cross-compilation flags needed. First build takes ~5-8 min (downloading + compiling
dependencies); subsequent builds are ~30 s.

### 4. Start Mosquitto

```powershell
docker compose -f compose.native.yaml up -d mosquitto
```

### 5. Serve the frontend

**Option 5a — Use Docker nginx** (simplest):

```powershell
docker compose -f compose.native.yaml up -d
```

**Option 5b — Use a simple static server** (no Docker nginx):

```powershell
cd frontend
npx serve dist -p 80
```

Or install nginx for Windows and point `root` to `frontend\dist\`.

### 6. Start wactorz

In PowerShell:

```powershell
# Load .env variables into the current session
Get-Content .\.env | Where-Object { $_ -match '^\s*[^#]' } | ForEach-Object {
    $parts = $_ -split '=', 2
    if ($parts.Count -eq 2) { [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process") }
}

.\rust\target\release\wactorz.exe --no-cli
```

Or in Git Bash:

```bash
source .env 2>/dev/null || true
./rust/target/release/wactorz.exe --no-cli
```

Open **http://localhost/** (nginx) or **http://localhost:3000** (Vite dev server).

---

## Cross-compiling for Linux deployment

If you develop on Windows but deploy to a Linux server, you need a Linux binary.

### Option X1 — Docker buildx (easiest, works on all Windows)

```powershell
docker buildx build --platform linux/amd64 --tag wactorz:linux --load .\rust
```

Extract the binary from the image:

```powershell
$id = docker create --platform linux/amd64 wactorz:linux
docker cp "${id}:/app/wactorz" .\wactorz-linux-amd64
docker rm $id
```

### Option X2 — WSL2 (fastest compilation, native Linux toolchain)

```powershell
# Install WSL2 with Ubuntu
wsl --install

# Inside WSL2:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source ~/.cargo/env
cd /mnt/c/Users/<your-name>/wactorz/rust
cargo build --release --bin wactorz
# → target/release/wactorz  (native linux/amd64 binary)
```

### Option X3 — Cross-compile to linux/arm64 (for ARM64 server targets)

```powershell
rustup target add aarch64-unknown-linux-gnu
# Install a cross-linker via the 'cross' tool:
cargo install cross
cross build --release --bin wactorz --target aarch64-unknown-linux-gnu
```

---

## Running bash scripts (`deploy.sh`, `package-native.sh`, etc.)

The `scripts/` directory contains bash (`.sh`) scripts. On Windows, run them via:

### Git Bash (simplest)

```bash
# Open Git Bash terminal, then:
cd /c/Users/<your-name>/wactorz
bash scripts/deploy.sh
```

### WSL2 (best compatibility)

```bash
# Inside WSL2 Ubuntu:
cd /mnt/c/Users/<your-name>/wactorz
bash scripts/deploy.sh
```

### PowerShell equivalent (no bash needed)

The three most common script tasks can be done directly in PowerShell:

```powershell
# Build frontend
cd frontend; npm run build; cd ..

# Build Rust binary
cd rust; cargo build --release --bin wactorz; cd ..

# rsync to remote (requires OpenSSH + rsync; easiest via Git Bash or WSL2)
# PowerShell alternative: use SCP
scp -r .\frontend\dist\ user@host:/opt/wactorz/frontend/
scp .\rust\target\release\wactorz user@host:/opt/wactorz/wactorz
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

## ARM64 Windows — summary

| Task | Status | Notes |
|---|---|---|
| `docker compose up -d` | ✅ | Runs linux/amd64 via QEMU; native arm64 with custom build |
| `npm install && npm run dev` | ✅ | Node.js has native ARM64 Windows builds |
| `cargo build --release` | ✅ | Produces native `aarch64-pc-windows-msvc` `.exe` |
| Cross-compile to linux/amd64 | ✅ slow | Docker buildx uses QEMU (~10-15 min first build) |
| Cross-compile to linux/arm64 | ✅ fast | `cross` + `aarch64-unknown-linux-gnu` target |
| `bash scripts/deploy.sh` | via WSL2 | Or Git Bash |
| NautilusAgent ssh | ✅ | Windows OpenSSH Client included |
| NautilusAgent rsync | via WSL2 | Or Chocolatey `rsync` |

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

### `cargo build` fails: `link.exe not found`

The MSVC linker is missing. Install Visual Studio Build Tools:

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools
```

During install, select **"Desktop development with C++"** workload.

### Port 80 already in use

IIS (Internet Information Services) often occupies port 80 on Windows.
Either stop IIS or change the port in `.env`:

```env
DASHBOARD_EXTERNAL_PORT=8080
```

Then open **http://localhost:8080/**.

To stop IIS temporarily:

```powershell
net stop w3svc
```

### `MQTT_HOST` connection refused

Ensure Mosquitto is running:

```powershell
docker compose -f compose.native.yaml ps
```

For native binary mode, set `MQTT_HOST=localhost` in `.env` (not `mosquitto`).

### ARM64: Docker image `exec format error`

The pulled image is `linux/amd64`; you need to enable QEMU emulation in Docker Desktop:
Settings → Docker Engine → add `"experimental": true`.
Or build a native `linux/arm64` image:

```powershell
docker buildx build --platform linux/arm64 --tag wactorz-server:arm64 --load .\rust
```

Then update `compose.yaml` to use `image: wactorz-server:arm64` and `platform: linux/arm64`.

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
winget install Git.Git Microsoft.WindowsTerminal OpenJS.NodeJS.LTS Rustlang.Rustup Docker.DockerDesktop

# 2. Enable WSL2 (for bash scripts and rsync)
wsl --install   # installs Ubuntu by default; reboot when prompted

# 3. Clone the repo
git clone https://github.com/waldiez/wactorz
cd wactorz
copy .env.example .env
notepad .env   # set LLM_API_KEY

# 4. Start the mock dev stack (no Rust build needed)
docker compose -f compose.dev.yaml up -d
cd frontend && npm install && npm run dev

# → http://localhost:3000  ✓
```

To move to a full stack later, build in WSL2 (faster than QEMU cross-compile):

```bash
# In WSL2:
cd /mnt/c/Users/<you>/wactorz/rust
cargo build --release --bin wactorz
# Produces native linux/amd64 binary for deployment
```
