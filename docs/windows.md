# Wactorz on Windows

This guide covers x86-64 and ARM64 Windows machines, including Snapdragon X,
Surface Pro X, and Copilot+ PCs.

---

## Which option fits you?

| Option | Docker needed | Node.js needed | Best for |
|---|---|---|---|
| **A - Docker Compose** | yes | no | Full local stack from a repo clone |
| **B - Docker Hub image** | yes | no | Fastest run without building locally |
| **C - Frontend dev mode** | yes | yes | Dashboard development with mock agents |

Start with Option A if you are working from the repository. Use Option B if you
only want to run Wactorz. Use Option C when you are editing the frontend.

---

## Prerequisites

### Install Windows Terminal

```powershell
winget install Microsoft.WindowsTerminal
```

### Install Git

```powershell
winget install Git.Git
```

Git for Windows includes Git Bash, which is useful for shell scripts and Unix-like
commands.

### Install Docker Desktop

```powershell
winget install Docker.DockerDesktop
```

After install, open Docker Desktop and wait for the engine to start.

ARM64 Windows can run `linux/arm64` containers natively and `linux/amd64`
containers through emulation. Prefer the published multi-arch images when they
are available.

### Install Node.js for frontend development

```powershell
winget install OpenJS.NodeJS.LTS
```

Node.js is only needed for Option C.

---

## Option A - Docker Compose from the repository

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz
copy .env.template .env
notepad .env
```

Set `LLM_API_KEY` for cloud providers, or configure Ollama in `.env`.

Start the Python stack:

```powershell
docker compose --profile python up -d
```

Open:

- Dashboard: http://localhost:8888
- REST API: http://localhost:8000
- Prometheus: http://localhost:9090

Stop the stack:

```powershell
docker compose down
```

To include the bundled Home Assistant dev container:

```powershell
docker compose --profile full up -d
```

---

## Option B - Docker Hub image

Use the Docker Hub quickstart when you do not need a repo clone:

[Quickstart: Docker Hub](dockerhub.md)

This is the simplest option for trying Wactorz on Windows.

---

## Option C - Frontend dev mode

The mock stack publishes realistic MQTT activity so you can develop the dashboard
without a live LLM key or Home Assistant instance.

Terminal 1:

```powershell
docker compose -f compose.dev.yaml up -d
```

Terminal 2:

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:3000.

To stop the mock stack:

```powershell
docker compose -f compose.dev.yaml down
```

---

## Running Shell Scripts on Windows

Use Git Bash or WSL2 for repository scripts that expect a Unix shell.

Git Bash:

```bash
cd /c/Users/<your-name>/wactorz
./run.sh
```

WSL2:

```bash
cd /mnt/c/Users/<your-name>/wactorz
./run.sh
```

PowerShell can run Docker and npm commands directly, but it does not execute
`.sh` scripts without a shell.

---

## SSH Keys for Remote Nodes

Deploying an edge node with `/deploy` connects over SSH, and key auth is
preferred over a password. Windows 10 and 11 include OpenSSH Client, so generate
a dedicated key:

```powershell
ssh-keygen -t ed25519 -C "wactorz-deploy" -f "$env:USERPROFILE\.ssh\wactorz_deploy" -N '""'
```

Copy the public key to the remote host:

```powershell
type "$env:USERPROFILE\.ssh\wactorz_deploy.pub" | ssh pi@192.168.1.50 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Point the node's deploy target at it in `.env`:

```env
DEPLOY_TARGETS=rpi-kitchen
DEPLOY_RPI_KITCHEN_HOST=192.168.1.50
DEPLOY_RPI_KITCHEN_USER=pi
DEPLOY_RPI_KITCHEN_KEY=~/.ssh/wactorz_deploy
DEPLOY_RPI_KITCHEN_BROKER=192.168.1.10
```

See [Remote nodes](remote-nodes.md) for the rest of the setup.

---

## Environment Variable Gotchas

Save `.env` as UTF-8 without BOM. VS Code and modern Notepad are usually fine.

Use forward slashes or escaped backslashes for paths:

```env
DEPLOY_RPI_KITCHEN_KEY=~/.ssh/wactorz_deploy
DEPLOY_RPI_KITCHEN_KEY=C:/Users/alice/.ssh/wactorz_deploy
```

Avoid unescaped Windows backslashes:

```env
DEPLOY_RPI_KITCHEN_KEY=C:\Users\alice\.ssh\wactorz_deploy
```

If a shell script reads `.env`, LF line endings are safest. In VS Code, click the
line-ending indicator in the status bar and choose `LF`.

---

## Troubleshooting

### `docker: command not found` in Git Bash

Docker Desktop may be available in PowerShell but not Git Bash. Run Docker
commands in PowerShell, or add Docker to Git Bash's `PATH`:

```bash
export PATH="$PATH:/c/Program Files/Docker/Docker/resources/bin"
```

### Docker Desktop is not running

Open Docker Desktop from the Start menu and wait for the engine to start. Then
rerun the Docker command.

### Port already in use

Change the host port in `.env` or stop the process using that port. For example:

```env
DASHBOARD_EXTERNAL_PORT=8080
```

Then open http://localhost:8080.

### `MQTT_HOST` connection refused

For Docker Compose, use the service name inside the Docker network:

```env
MQTT_HOST=mosquitto
```

For a local Python process talking to a broker published on the host, use:

```env
MQTT_HOST=localhost
```

Check the broker container:

```powershell
docker compose ps
```

### ARM64 image issues

If Docker reports an architecture error, update Docker Desktop and make sure it
can run emulated Linux containers. Prefer published multi-arch images where
possible.

---

## Recommended ARM64 Windows Setup

```powershell
winget install Git.Git Microsoft.WindowsTerminal OpenJS.NodeJS.LTS Docker.DockerDesktop
wsl --install

git clone https://github.com/waldiez/wactorz
cd wactorz
copy .env.template .env
notepad .env

docker compose --profile python up -d
```

For frontend work:

```powershell
docker compose -f compose.dev.yaml up -d
cd frontend
npm install
npm run dev
```
