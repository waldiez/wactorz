# Deployment

Wactorz supports three deployment modes:

| Mode | When to use |
|---|---|
| **Docker Hub** | New users; no repo clone needed — just Docker Desktop |
| **Full Docker** | Full stack via `git clone`; everything in containers |
| **Home Assistant add-on** | Home Assistant OS or Supervised installs |

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
| _(all)_ | mosquitto | `mosquitto:1883` | `:1883` |
| `python` | wactorz-python | `wactorz-python:8000` | `:8000` (REST API) |
| `python` | monitor UI | `wactorz-python:8888` | `:8888` |
| `python` | prometheus | `wactorz-prometheus:9090` | `:9090` |
| `full` | home-assistant | `homeassistant:8123` | `:8123` |

```bash
# Python stack (most common)
docker compose --profile python up -d
# Open: http://localhost:8888  (monitor UI)  http://localhost:8000  (REST API)
```

---

## Home Assistant add-on

Use the add-on when Wactorz should run inside Home Assistant OS or a Supervised
Home Assistant install. The add-on uses prebuilt multi-arch images from GHCR, so
Supervisor updates pull an image instead of building Wactorz on the device.

See `ha-addon/wactorz/README.md` (or `ha-addon/wactorz-ultra/README.md` for the ML variant) for install and local testing details.

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
| `MQTT_USERNAME` | `wactorz` | Broker username. Blank only for a broker of your own that takes anonymous connections |
| `MQTT_PASSWORD` | _(none)_ | Broker password. **Required by docker compose** — the bundled broker refuses anonymous connections and compose refuses to start without it, rather than coming up open. Its password file is generated from these at container start, so there is no `mosquitto_passwd` step |
| `PORT` | `8000` | Python REST API listen port |
| `WS_PORT` / `MONITOR_PORT` | `8888` | Web UI / monitor server port |
| `WACTORZ_STATE_DIR` | `./state` | Where all durable state lives — SQLite database, per-agent pickles, MQTT outbox. Set an absolute path when the working directory isn't durable (a container without a mounted volume loses it on restart); the Home Assistant add-on pins `/data/state`. `wactorz-reset` reads the same variable, so a wipe targets whatever the app is using |
| `WACTORZ_TZ` | _(unset)_ | Override the timezone used in agents' date/time context (e.g. `Europe/Athens`). Precedence: a user's `pref_timezone` fact > `WACTORZ_TZ` > standard `TZ` > host local zone. Blank or unknown values fall through to the next candidate |
| `PROMETHEUS_EXTERNAL_PORT` | `9090` | Prometheus host port |
| `PROMETHEUS_SCRAPE_INTERVAL` | `15s` | Global Prometheus scrape interval |
| `PROMETHEUS_MONITOR_MOSQUITTO` | `1` | Enable Mosquitto TCP availability probe |
| `DEPLOY_TARGETS` | _(unset)_ | Comma-separated remote node names `/deploy` may bootstrap; each needs a `DEPLOY_<NODE>_*` block — see [Remote nodes](remote-nodes.md) |
| `DEPLOY_KNOWN_HOSTS` | `<WACTORZ_STATE_DIR>/known_hosts` | Where learned SSH host keys are stored |
| `DEPLOY_STRICT_HOST_KEYS` | `0` | `1` = never learn a host key on first contact; unknown hosts are refused |

---

## SSH key management

Wactorz reaches remote machines over SSH when bootstrapping an edge node with
`/deploy`. Key auth is preferred over a password — generate a dedicated deploy
key:

```bash
ssh-keygen -t ed25519 -C "wactorz-deploy" -f ~/.ssh/wactorz_deploy -N ""

# Authorise on the target host
ssh-copy-id -i ~/.ssh/wactorz_deploy.pub -p 22 pi@192.168.1.50
```

Then point the node's deploy target at it in `.env`:

```env
DEPLOY_TARGETS=rpi-kitchen
DEPLOY_RPI_KITCHEN_HOST=192.168.1.50
DEPLOY_RPI_KITCHEN_USER=pi
DEPLOY_RPI_KITCHEN_KEY=~/.ssh/wactorz_deploy
DEPLOY_RPI_KITCHEN_BROKER=192.168.1.10
```

Credentials are read from here and never from chat — `/deploy` takes a node name
and nothing else. Host keys are verified on every connection, learned on first
contact unless `DEPLOY_STRICT_HOST_KEYS=1`. Full details in
[Remote nodes](remote-nodes.md).

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
    # The endpoint reads `message`, plus an optional `agent_name` that defaults
    # to the orchestrator. Add `"agent_name": "<name>"` to address one agent.
    payload: '{"message":"{{ message }}"}'
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

The `full` profile (`docker compose --profile full up -d`) starts a fresh Home Assistant container alongside Wactorz on the same Docker network — useful for a clean dev environment, not for connecting to an existing production HA.

> **Home Assistant OS / Supervised users** — use the [Wactorz HA addon](https://github.com/waldiez/wactorz/tree/main/ha-addon) instead. It runs inside the Supervisor and connects to your existing HA instance automatically.
