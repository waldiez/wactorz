# Wactorz on Windows

This guide covers x86-64 and ARM64 Windows machines, including Snapdragon X,
Surface Pro X and Copilot+ PCs.

---

## Which option fits you?

| Option | Needs | Best for |
|---|---|---|
| **A - Docker Compose** | Git, Docker Desktop | The full stack from a repository clone: broker, Wactorz, Prometheus |
| **B - Docker Hub image** | Docker Desktop | Running Wactorz without cloning anything |
| **C - pip install** | Python 3.11 or later, Docker Desktop for the broker | Running Wactorz natively, or using it as a library |
| **D - Frontend development** | Git, Docker Desktop, Python 3.11 or later, Bun | Working on the dashboard |

Start with Option A if you want everything running from the repository. Use
Option B if you only want to run Wactorz. Use Option C to install the package into
a Python environment of your own, and Option D when you are editing the dashboard.

---

## Prerequisites

Install only what your option needs. Each line runs in PowerShell or Windows
Terminal.

```powershell
winget install Microsoft.WindowsTerminal
winget install Git.Git
winget install Docker.DockerDesktop
winget install Python.Python.3.13      # Options C and D
winget install Oven-sh.Bun             # Option D
```

After Docker Desktop is installed, open it and wait for the engine to start.

Wactorz needs **Python 3.11 or later**. Check which one you have with
`py --version`, and see every installed version with `py --list`. Python 3.10 and
earlier cannot install current releases: pip reports
`requires a different Python`.

ARM64 Windows runs `linux/arm64` containers natively and `linux/amd64` containers
through emulation. The published Wactorz images are multi-arch, so Docker picks the
native one.

---

## Option A - Docker Compose from the repository

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz
Copy-Item .env.template .env
notepad .env
```

Set at least these values in `.env`:

```dotenv
MQTT_PASSWORD=choose-a-long-random-password
LLM_PROVIDER=anthropic
LLM_API_KEY=your-key-here
```

The broker refuses to start without `MQTT_PASSWORD`. To use a local model instead
of a cloud provider, set `LLM_PROVIDER=ollama` (see [Installation](development.md)).

Start the stack:

```powershell
docker compose --profile python up -d
```

Open:

- Dashboard: http://localhost:8888
- REST API: http://localhost:8000
- Prometheus: http://localhost:9090

Follow the logs with `docker compose logs -f wactorz-python`, and stop the stack
with:

```powershell
docker compose --profile python down
```

To include the bundled Home Assistant development container, use
`--profile full` instead of `--profile python`.

To update, pull the repository and rebuild:

```powershell
git pull
docker compose --profile python up -d --build
```

---

## Option B - Docker Hub image

Use the Docker Hub guide when you do not need a repository clone:
[Docker Hub](dockerhub.md). It is the simplest way to try Wactorz on Windows.

---

## Option C - pip install

Make a virtual environment with Python 3.11 or later, and install Wactorz into it:

```powershell
py -3.13 -m venv $env:USERPROFILE\wactorz-venv
& "$env:USERPROFILE\wactorz-venv\Scripts\python.exe" -m pip install "wactorz[all]"
```

Calling the environment's own `python.exe` avoids PowerShell's script execution
policy, which can block `Activate.ps1`.

Wactorz needs an MQTT broker. For one on this machine only, run Mosquitto in
Docker with its no-authentication configuration, published on loopback:

```powershell
docker run -d --name mosquitto -p 127.0.0.1:1883:1883 eclipse-mosquitto:2.1.2-alpine mosquitto -c /mosquitto-no-auth.conf
```

Then start Wactorz, with your provider's key in the environment or in a `.env`
file in the folder you start it from:

```powershell
$env:LLM_API_KEY = "your-key-here"
& "$env:USERPROFILE\wactorz-venv\Scripts\wactorz.exe"
```

Open the dashboard at http://localhost:8888. A broker without authentication is
only safe while it is published on `127.0.0.1`; before you connect other machines
to it, set up accounts as described in [Security](security.md).

To upgrade, run the same `pip install` with `--upgrade`.

---

## Option D - Frontend development

The development stack runs the broker and the backend in containers, with the
development credentials (`wactorz` / `wactorz-dev`) already filled in:

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz
Copy-Item .env.template .env
notepad .env       # set LLM_API_KEY
docker compose -f compose.dev.yaml --profile app up -d
```

Then start Vite with hot reload:

```powershell
cd frontend
bun install
bun run dev
```

Open http://localhost:3000. Vite sends API and WebSocket calls to the backend on
port 8888.

Stop the stack with:

```powershell
docker compose -f compose.dev.yaml --profile app down
```

Without `--profile app`, `compose.dev.yaml` starts only the broker. Use that to run
the backend from the repository on the host instead, which needs Python 3.11 or
later and `pip install -e ".[all]"`. The Makefile's `make dev-full` does this, and
needs Git Bash or WSL2 (see below).

---

## Running shell scripts and make on Windows

The repository's `run.sh` and the Makefile expect a Unix shell. Use Git Bash or
WSL2 for them.

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

PowerShell runs Docker, Python and Bun commands directly, but it does not execute
`.sh` scripts without a shell.

---

## SSH keys for remote nodes

Deploying an edge node with `/deploy` connects over SSH, and key auth is
preferred over a password. Windows 10 and 11 include the OpenSSH client, so
generate a dedicated key:

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

The node needs Python 3.11 or later too. See [Remote nodes](remote-nodes.md) for
the rest of the setup.

---

## Environment variable gotchas

Save `.env` as UTF-8 without BOM. VS Code and current Notepad do this by default.

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

### `requires a different Python`

pip is running under Python 3.10 or earlier. Install Python 3.11 or later, then
make the virtual environment again with it (`py -3.13 -m venv ...`).

### `set MQTT_PASSWORD in .env`

Docker Compose stops before starting the broker because `MQTT_PASSWORD` is empty.
Set it in `.env` and run the command again.

### `docker: command not found` in Git Bash

Docker Desktop may be available in PowerShell but not Git Bash. Run Docker
commands in PowerShell, or add Docker to Git Bash's `PATH`:

```bash
export PATH="$PATH:/c/Program Files/Docker/Docker/resources/bin"
```

### Docker Desktop is not running

Open Docker Desktop from the Start menu and wait for the engine to start. Then
run the Docker command again.

### Port already in use

Change the port in `.env`, or stop the process using it. For the dashboard:

```env
MONITOR_PORT=8080
```

Then open http://localhost:8080. `REST_EXTERNAL_PORT`, `PROMETHEUS_EXTERNAL_PORT`
and `MQTT_EXTERNAL_PORT` move the other published ports.

### `MQTT_HOST` connection refused

Inside Docker Compose, Wactorz reaches the broker by its service name, which the
compose file sets for you. For Wactorz running on the host, against a broker
published on the host, use:

```env
MQTT_HOST=localhost
```

Check the broker container with `docker compose ps`.

### ARM64 image issues

If Docker reports an architecture error, update Docker Desktop and make sure it can
run emulated Linux containers.

---

## Recommended ARM64 Windows setup

```powershell
winget install Git.Git Microsoft.WindowsTerminal Docker.DockerDesktop Python.Python.3.13 Oven-sh.Bun
wsl --install

git clone https://github.com/waldiez/wactorz
cd wactorz
Copy-Item .env.template .env
notepad .env       # set MQTT_PASSWORD and LLM_API_KEY

docker compose --profile python up -d
```

For frontend work, continue with [Option D](#option-d-frontend-development).
