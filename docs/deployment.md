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

The image comes in two sizes: `waldiez/wactorz:latest`, and `waldiez/wactorz:ultra` with
PyTorch, Ultralytics, OpenCV and what the Reachy Mini SDK needs. [Which image](dockerhub.md#which-image)
says when the larger one is the one to pull.

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

Compose builds the image from the checkout. For vision agents or the Reachy Mini agent,
build the larger one ([which image](dockerhub.md#which-image)) by setting
`WACTORZ_FLAVOUR=ultra` in `.env`, then:

```bash
docker compose --profile python up -d --build
```

Both ask for the API key. With `API_KEY` blank in `.env`, the stack generates one
on first start and keeps it in a volume. Read it with
`docker compose exec wactorz-python cat /run/wactorz/api_key`, or follow the
one-time sign-in link in `docker compose logs wactorz-python`. Set `API_KEY` in
`.env` to choose your own.

### Services

Default profile (no flag) starts Mosquitto only. Add `--profile` flags to bring up more services.

| Profile | Service | Internal address | External port |
|---|---|---|---|
| _(all)_ | mosquitto | `mosquitto:1883` | `127.0.0.1:1883`, and `:8883` (TLS) |
| `python` | wactorz-python | `wactorz-python:8000` | `127.0.0.1:8000` (REST API) |
| `python` | monitor UI | `wactorz-python:8888` | `127.0.0.1:8888` |
| `python` | prometheus | `wactorz-prometheus:9090` | `127.0.0.1:9090` |
| `python` | alertmanager | `alertmanager:9093` | `127.0.0.1:9093` |
| `full` | home-assistant | `homeassistant:8123` | `127.0.0.1:8123` |

Every port except the broker's TLS one is published to this host only. Reach the
dashboard and the API from elsewhere through a TLS proxy; `HA_EXTERNAL_BIND=0.0.0.0`
opens Home Assistant to the network, and `MQTT_EXTERNAL_BIND=0.0.0.0` the plain
broker port.

Each container has a ceiling on memory and on process ids, so one that leaks is
restarted instead of exhausting the host. The app's are settings, because what
an agent loads varies: `WACTORZ_MEM_LIMIT` (default `8g`), `WACTORZ_PIDS_LIMIT`
(`4096`, threads included) and `WACTORZ_CPUS` (cores; `0`, the default, is no
limit). An app container that restarts under a heavy agent, with `OOMKilled` in
`docker inspect`, needs `WACTORZ_MEM_LIMIT` raised. Home Assistant's container
has none.

```bash
# Python stack (most common)
docker compose --profile python up -d
# Open: http://localhost:8888  (monitor UI)  http://localhost:8000  (REST API)
```

### Health probes

Both servers answer the same probes, with no key:

- `/health` (also `/healthz`, `/livez`) is **liveness**. It fails only when the
  process cannot answer, which is what the compose files and the image's
  `HEALTHCHECK` restart on.
- `/ready` (also `/readyz`) is **readiness**. It answers `503` while the agents
  start or stop, and while the broker or the database is unreachable.

On Kubernetes, point each probe at its own path, and give liveness a start
period that covers startup:

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8888 }
  initialDelaySeconds: 60
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8888 }
  periodSeconds: 10
```

Never use `/ready` for liveness. A broker outage would then restart every
replica in a loop, and restarting fixes nothing the broker's return would not.
See [the API reference](api.md) for what each check means.

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
| `WACTORZ_LOG_FORMAT` | `text` | `json` writes each log record as one JSON object on one line (JSON Lines), to the console and to `wactorz.log`, for a collector that parses logs: fields `ts` (UTC), `level`, `logger`, `message`, and `exception` holding the whole traceback. Redaction applies as in text. The dashboard's log view is unaffected. A node reads the setting from its own environment |
| `WACTORZ_RETENTION_CHAT_DAYS` | `365` | Days chat history is kept; `0` keeps it for ever. An attached file goes with the last message that refers to it, or a day after upload if it was never sent |
| `WACTORZ_RETENTION_TIMESERIES_DAYS` | `365` | Days sensor readings, detections, Home Assistant state changes and actuations are kept; `0` keeps them for ever. The time-series collector agent's own `retention_days` applies too, and the shorter window holds |
| `WACTORZ_RETENTION_OUTBOX_DAYS` | `7` | Days an MQTT message the broker never accepted stays in the outbox; `0` keeps it until delivered. Once expired it is not retried after a restart, and the log names its topic. A command — a non-retained message under `nodes/` or `agents/by-name/`, such as a spawn, a stop or a task for an agent — expires after 10 minutes whatever this says, since replaying one later would undo or repeat what has happened since; a node's retained `desired_state` follows this setting |
| `PROMETHEUS_EXTERNAL_PORT` | `9090` | Prometheus host port |
| `ALERTMANAGER_EXTERNAL_PORT` | `9093` | Alertmanager host port |
| `ALERT_WEBHOOK_URL` | _(none)_ | Compose only: where Alertmanager POSTs alerts. Unset, alerts are listed on its page and sent nowhere. See `prometheus.md` |
| `ALERT_WEBHOOK_TOKEN` | _(none)_ | Compose only: sent to that webhook as a bearer token |
| `HA_EXTERNAL_BIND` / `HA_EXTERNAL_PORT` | `127.0.0.1` / `8123` | Where compose publishes Home Assistant (profile `full`). `0.0.0.0` opens it to the network |
| `WACTORZ_MEM_LIMIT` | `8g` | Compose only: the app container's memory ceiling |
| `WACTORZ_PIDS_LIMIT` | `4096` | Compose only: the app container's ceiling on processes and threads |
| `WACTORZ_CPUS` | `0` | Compose only: cores the app container may use; `0` is no limit |
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
