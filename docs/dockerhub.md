# Quickstart: Docker Hub

The fastest way to run Wactorz — no repo clone needed. Everything runs in containers pulled straight from Docker Hub.

> **Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.

---

- [Which image](#which-image)
- [Option A — Terminal + Compose (recommended)](#option-a--terminal--compose-recommended)
- [Option B — Docker Desktop + Terminal](#option-b--docker-desktop--terminal)

---

## Which image

There are two, built from the same source and published under the same name:

| Tag | What it holds | When to use it |
| --- | --- | --- |
| `waldiez/wactorz:latest`, `:<version>` | Wactorz and its integrations (the `all` extra). | Agents that talk to APIs, Home Assistant, MQTT, chat platforms. This is the one the examples here use. |
| `waldiez/wactorz:ultra`, `:<version>-ultra` | The same, plus PyTorch (the CPU build), Ultralytics and OpenCV, the system libraries they load, a compiler, and GStreamer with its introspection data. | Vision agents (anything that imports `cv2`, `torch` or `ultralytics`), and the Reachy Mini catalogue agent, whose SDK has to build part of itself. |

The larger one is several times the size of the other, which is the reason there are
two. An agent that needs it fails in the small image when it imports `cv2`
(`libGL.so.1: cannot open shared object file`) or when pip tries to build a package that
ships no wheel. To switch, change the tag in `compose.yaml` and start again; the state
volume is the same for both.

The `ultra` image runs on Python 3.13, one release behind the other, because the Reachy
Mini SDK needs it. It has no GPU support: PyTorch in it is the CPU build.

### Licences

Wactorz is Apache-2.0, and so is everything in the default image. The `ultra` image also
contains [Ultralytics](https://github.com/ultralytics/ultralytics), which is licensed
under the **AGPL-3.0**, so the `ultra` image as a whole is distributed under its terms.
Using the `ultra` image, or Ultralytics through it, in a closed-source product or a hosted
service means meeting the AGPL-3.0 (offering the source of the whole to its users) or
holding an [Ultralytics licence](https://www.ultralytics.com/license). Wactorz's own
code stays Apache-2.0 either way, and the default image has no such component.

The corresponding source is the source of this release at <https://github.com/waldiez/wactorz> (its tag, with the Dockerfile that builds the image) and Ultralytics' own at <https://github.com/ultralytics/ultralytics>. Every package in an image is listed with its licence in
`/app/THIRD_PARTY.txt`.

---

## Option A — Terminal + Compose (recommended)

Works in any terminal, including the built-in terminal inside Docker Desktop.

### 1. Create a project folder

```bash
mkdir wactorz
cd wactorz
```

### 2. Make two secrets

The server will not start without an API key: it listens on the network inside
its container, and anything that reaches it could otherwise drive your agents
and spend your LLM budget. The broker needs a password for the same reason. Run
this twice and keep both lines it prints — it uses the Wactorz image's own
Python, so it works the same on Linux, macOS and Windows:

```bash
docker run --rm --entrypoint python waldiez/wactorz:latest -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Create three files inside that folder

> **Windows tip:** open Notepad, paste the content, then *Save As* — set *Save as type* to **All Files** and type the filename exactly as shown. This prevents Windows from secretly adding `.txt` to the end.

**`mosquitto.conf`**

```
listener 1883
allow_anonymous false
password_file /mosquitto/data/passwd
persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

**`compose.yaml`**

```yaml
name: wactorz

services:
  mosquitto:
    image: eclipse-mosquitto:2.1.2-alpine@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408
    container_name: wactorz-mosquitto
    restart: unless-stopped
    environment:
      MQTT_USERNAME: ${MQTT_USERNAME:-wactorz}
      MQTT_PASSWORD: ${MQTT_PASSWORD:?set MQTT_PASSWORD in .env}
    # The password file is written from .env each time the broker starts, into
    # its own volume, so changing the password is an edit to .env and a restart.
    command:
      - /bin/sh
      - -c
      - |
        umask 077
        mosquitto_passwd -b -c /mosquitto/data/passwd "$$MQTT_USERNAME" "$$MQTT_PASSWORD"
        chown mosquitto:mosquitto /mosquitto/data/passwd
        exec mosquitto -c /mosquitto/config/mosquitto.conf
    ports:
      - "127.0.0.1:1883:1883"
    volumes:
      - ./mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - mosquitto-data:/mosquitto/data
    networks:
      - wactorz-net
    healthcheck:
      test: ["CMD-SHELL", "mosquitto_sub -u \"$$MQTT_USERNAME\" -P \"$$MQTT_PASSWORD\" -t '$$SYS/#' -C 1 -i hc -W 3"]
      interval: 10s
      timeout: 5s
      retries: 5

  wactorz:
    image: waldiez/wactorz:latest
    container_name: wactorz
    restart: unless-stopped
    env_file:
      - .env
    environment:
      MQTT_HOST: mosquitto
      MQTT_PORT: "1883"
      INTERFACE: rest
    ports:
      - "127.0.0.1:8000:8000"
      - "127.0.0.1:8888:8888"
    networks:
      - wactorz-net
    depends_on:
      mosquitto:
        condition: service_healthy

networks:
  wactorz-net:

volumes:
  mosquitto-data:
```

The ports are published on this machine only (`127.0.0.1`). To reach the dashboard
from another device, remove `127.0.0.1:` from the two `wactorz` lines; the API key
is what then keeps it yours.

**`.env`** — the two secrets from step 2, then the provider you want to use:

```bash
# ── Access ─────────────────────────────────────────────────────────────────────
API_KEY=paste-the-first-secret-here
MQTT_USERNAME=wactorz
MQTT_PASSWORD=paste-the-second-secret-here

# ── Anthropic (Claude) — default ─────────────────────────────────────────────
LLM_API_KEY=sk-ant-...
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6

# ── OpenAI ────────────────────────────────────────────────────────────────────
# LLM_API_KEY=sk-...
# LLM_PROVIDER=openai
# LLM_MODEL=gpt-4o
# OPENAI_URL=  # optional: set to redirect to a compatible endpoint (Groq, Together, vLLM…)

# ── Ollama (local) ───────────────────────────────────────────────────────────
# LLM_PROVIDER=ollama
# LLM_MODEL=llama3
```

### 4. Start

```bash
docker compose up -d
```

Images are pulled automatically on first run.

### 5. Open

| | URL |
|---|---|
| Monitor UI | `http://localhost:8888` — sign in with the `API_KEY` from `.env` |
| REST API | `http://localhost:8000` — send it as the `X-API-Key` header |

To stop: `docker compose down`

---

## Option B — Docker Desktop + Terminal

The same setup with `docker run`, in PowerShell.

### Step 1 — Create a project folder

Use a new folder so the `.env` and `mosquitto.conf` paths are easy to copy into Docker commands:

```powershell
mkdir wactorz
cd wactorz
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/waldiez/wactorz/main/.env.template" `
  -OutFile ".env.template"
Copy-Item .env.template .env
```

### Step 2 — Edit `.env`

Make two secrets — run this twice and keep both lines it prints:

```powershell
docker run --rm --entrypoint python waldiez/wactorz:latest -c "import secrets; print(secrets.token_hex(32))"
```

```powershell
notepad .env
```

Fill in your LLM key and provider, put the first secret in `API_KEY` and the second
in `MQTT_PASSWORD`, and make sure these Docker-specific values are set:

```bash
API_KEY=paste-the-first-secret-here
MQTT_HOST=wactorz-mosquitto
MQTT_USERNAME=wactorz
MQTT_PASSWORD=paste-the-second-secret-here
PORT=8000
MONITOR_PORT=8888
```

> **Port conflict?** On some Windows machines port `8888` is reserved by a system service. If the monitor UI is unreachable, change `MONITOR_PORT` to any free port (e.g. `8887`) and use that port in Step 4.

### Step 3 — Start Mosquitto

```powershell
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "mosquitto.conf"),
  "listener 1883`nallow_anonymous false`npassword_file /mosquitto/data/passwd`npersistence true`npersistence_location /mosquitto/data/`nlog_dest stdout`n",
  [System.Text.UTF8Encoding]::new($false)
)

docker network create wactorz-net
docker volume create wactorz-mosquitto-data
docker run -d --name wactorz-mosquitto `
  --network wactorz-net `
  -p "127.0.0.1:1883:1883" `
  --env-file "${PWD}\.env" `
  -v "${PWD}\mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" `
  -v "wactorz-mosquitto-data:/mosquitto/data" `
  eclipse-mosquitto:2.1.2-alpine@sha256:38c0da4f2ef84284d47b3b3eeea1cb3bdeabe81ee10caf0cd5c5ff61ee3ea408 `
  /bin/sh -c 'umask 077; mosquitto_passwd -b -c /mosquitto/data/passwd "$MQTT_USERNAME" "$MQTT_PASSWORD" && chown mosquitto:mosquitto /mosquitto/data/passwd && exec mosquitto -c /mosquitto/config/mosquitto.conf'
```

If `wactorz-net` already exists, the `network create` line will error — that is OK.

### Step 4 — Start Wactorz

```powershell
docker run -d --name wactorz `
  --network wactorz-net `
  -p "127.0.0.1:8000:8000" `
  -p "127.0.0.1:8888:8888" `
  --env-file "${PWD}\.env" `
  -e MQTT_HOST=wactorz-mosquitto `
  waldiez/wactorz:latest
```

Open `http://localhost:8888` and sign in with the `API_KEY` from `.env`.
