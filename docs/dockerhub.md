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

---

## Option A — Terminal + Compose (recommended)

Works in any terminal, including the built-in terminal inside Docker Desktop.

### 1. Create a project folder

```bash
mkdir wactorz
cd wactorz
```

### 2. Create three files inside that folder

> **Windows tip:** open Notepad, paste the content, then *Save As* — set *Save as type* to **All Files** and type the filename exactly as shown. This prevents Windows from secretly adding `.txt` to the end.

**`mosquitto.conf`**

```
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

**`compose.yaml`**

```yaml
name: wactorz

services:
  mosquitto:
    image: eclipse-mosquitto:2.0
    container_name: wactorz-mosquitto
    restart: unless-stopped
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - mosquitto-data:/mosquitto/data
    networks:
      - wactorz-net
    healthcheck:
      test: ["CMD", "mosquitto_sub", "-t", "$$SYS/#", "-C", "1", "-i", "hc", "-W", "3"]
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
      - "8000:8000"
      - "8888:8888"
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

**`.env`** — uncomment the provider you want to use:

```bash
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

### 3. Start

```bash
docker compose up -d
```

Images are pulled automatically on first run.

### 4. Open

| | URL |
|---|---|
| Monitor UI | `http://localhost:8888` |
| REST API | `http://localhost:8000` |

To stop: `docker compose down`

---

## Option B — Docker Desktop + Terminal

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

```powershell
notepad .env
```

Fill in at minimum your LLM key and provider. Make sure these Docker-specific values are set:

```bash
MQTT_HOST=wactorz-mosquitto
PORT=8000
MONITOR_PORT=8888
```

> **Port conflict?** On some Windows machines port `8888` is reserved by a system service. If the monitor UI is unreachable, change `MONITOR_PORT` to any free port (e.g. `8887`) and use that port in Step 4.

### Step 3 — Start Mosquitto

```powershell
[System.IO.File]::WriteAllText(
  (Join-Path (Get-Location) "mosquitto.conf"),
  "listener 1883`nallow_anonymous true`npersistence true`npersistence_location /mosquitto/data/`nlog_dest stdout`n",
  [System.Text.UTF8Encoding]::new($false)
)

docker network create wactorz-net
docker run -d --name wactorz-mosquitto `
  --network wactorz-net `
  -p "1883:1883" `
  -v "${PWD}\mosquitto.conf:/mosquitto/config/mosquitto.conf" `
  eclipse-mosquitto:2.0
```

If `wactorz-net` already exists, the `network create` line will error — that is OK.

### Step 4 — Start Wactorz

```powershell
docker run -d --name wactorz `
  --network wactorz-net `
  -p "8000:8000" `
  -p "8888:8888" `
  --env-file "${PWD}\.env" `
  -e MQTT_HOST=wactorz-mosquitto `
  waldiez/wactorz:latest
```

Open `http://localhost:8888`.
