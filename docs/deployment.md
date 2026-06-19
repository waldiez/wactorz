# Deployment

Wactorz supports two deployment modes:

| Mode | When to use |
|---|---|
| **Docker Hub** | New users; no repo clone needed — just Docker Desktop |
| **Full Docker** | Full stack via `git clone`; everything in containers |

For Home Assistant OS / Supervised, use the [Wactorz HA addon](https://github.com/waldiez/wactorz/tree/main/ha-addon) instead.

---

## Docker Hub

The fastest way to get started — no repo clone or Python needed. See the dedicated guide:

→ **[Quickstart: Docker Hub](dockerhub.md)**

---

## Full Docker  (`compose.yaml`)

### Prerequisites

- Docker + Compose plugin
- `LLM_API_KEY` (Anthropic / OpenAI) or a local Ollama instance

### Steps

```bash
git clone https://github.com/waldiez/wactorz
cd wactorz
cp .env.template .env
nano .env           # set LLM_API_KEY at minimum

# Python stack (recommended starting point)
docker compose --profile python up -d
```

Open `http://localhost:8888` (monitor UI) or `http://localhost:8000` (REST API).

### Services

Default profile (no flag) starts Mosquitto only. Add `--profile` flags to bring up more services.

| Profile | Service | Internal address | External port |
|---|---|---|---|
| _(all)_ | mosquitto | `mosquitto:1883` / `:9001` | `:1883`, `:9001` |
| `python` / `python-full` | wactorz-python | `wactorz-python:8000` | `:8000` (REST API) |
| `python` / `python-full` | monitor UI | `wactorz-python:8888` | `:8888` |
| `python` / `python-full` | prometheus | `wactorz-prometheus:9090` | `:9090` |
| `python-full` | fuseki | `fuseki:3030` | `:3030` |
| `python-full` | home-assistant | `homeassistant:8123` | `:8123` |

```bash
# App + Fuseki + Home Assistant
docker compose --profile python-full up -d
```

---

## systemd service (persistent, starts on boot)

To run the Python backend directly on a host (with a containerised or host-native
Mosquitto), install the unit template:

```bash
sudo cp systemd/wactorz.service /etc/systemd/system/
sudo nano /etc/systemd/system/wactorz.service
# Edit: WorkingDirectory, EnvironmentFile, ExecStart, User

sudo systemctl daemon-reload
sudo systemctl enable --now wactorz
journalctl -u wactorz -f
```

The unit runs `run.sh` (which activates `./.venv` if present and starts
`python3 -m wactorz`). The template at `systemd/wactorz.service` has comments for
every field.

---

## Environment variables

See `.env.template` for the full annotated list.  The most important ones:

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `ollama` / `gemini` / `nim` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Any model ID |
| `LLM_API_KEY` | _(required for cloud providers)_ | API key — not needed for Ollama only |
| `OPENAI_URL` | _(unset)_ | Redirect `openai` provider to a compatible endpoint (Groq, Together, vLLM, etc.) |
| `LLM_COST_LIMIT_USD` | `0` (disabled) | Hard spend cap per period — set `0` to disable |
| `LLM_COST_LIMIT_PERIOD` | `monthly` | Reset period: `daily`, `weekly`, or `monthly` |
| `MQTT_HOST` | `localhost` | Use `mosquitto` inside Docker |
| `MQTT_PORT` | `1883` | |
| `PORT` | `8000` | Python REST API listen port |
| `WS_PORT` / `MONITOR_PORT` | `8888` | Web UI / monitor server port |
| `PROMETHEUS_EXTERNAL_PORT` | `9090` | Prometheus host port |
| `PROMETHEUS_SCRAPE_INTERVAL` | `15s` | Global Prometheus scrape interval |
| `PROMETHEUS_MONITOR_MOSQUITTO` | `1` | Enable Mosquitto TCP availability probe |
| `PROMETHEUS_MONITOR_FUSEKI` | `0` | Enable Fuseki HTTP availability probe |
| `FUSEKI_URL` | _(unset)_ | Fuseki SPARQL endpoint for the Knowledge Graph view |
| `FUSEKI_DATASET` | `wactorz` | Default dataset name |
| `FUSEKI_USER` / `FUSEKI_PASSWORD` | `admin` / _(unset)_ | Fuseki credentials (if auth is enabled) |
| `NAUTILUS_SSH_KEY` | _(default key)_ | Path to SSH private key |
| `NAUTILUS_STRICT_HOST_KEYS` | `0` | `1` = enforce strict host-key checking |
| `NAUTILUS_CONNECT_TIMEOUT` | `10` | SSH timeout in seconds |

---

## SSH key management (NautilusAgent)

Generate a dedicated key (recommended):

```bash
ssh-keygen -t ed25519 -C "wactorz-deploy" -f ~/.ssh/wactorz_deploy -N ""

# Authorise on the target host
ssh-copy-id -i ~/.ssh/wactorz_deploy.pub -p 22 user@host

# Add to .env
echo "NAUTILUS_SSH_KEY=~/.ssh/wactorz_deploy" >> .env
```

---

## Updating Home Assistant integration

Wactorz can send REST commands to Home Assistant and receive automations.

```yaml
# infra/homeassistant/configuration.yaml
rest_command:
  wactorz_chat:
    url: "http://wactorz-python:8000/api/chat"
    method: POST
    content_type: "application/json"
    payload: '{"to":"main-actor","content":"{{ message }}"}'
```

Set `HA_URL` and `HA_TOKEN` in `.env`.

---

## Connecting to an existing Home Assistant instance

If you already have Home Assistant running (in Docker or elsewhere), point Wactorz at it via `.env`:

```bash
# .env
HA_URL=http://192.168.1.x:8123   # or http://homeassistant.local:8123
HA_TOKEN=eyJ...                  # Long-lived access token from HA → Profile → Security
```

Then start only the Wactorz stack (no embedded HA):

```bash
docker compose --profile python up -d
```

The `python-full` profile starts a fresh Home Assistant container alongside Wactorz on
the same Docker network — useful for a clean dev environment, not for connecting to an
existing production HA.

> **Home Assistant OS / Supervised users** — use the [Wactorz HA addon](https://github.com/waldiez/wactorz/tree/main/ha-addon) instead. It runs inside the Supervisor and connects to your existing HA instance automatically.
