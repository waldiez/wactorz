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
| `sandbox` | wactorz-sandbox | — | none: it dials the broker |

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

### The sandbox node

Code the model writes can run in a container of its own, holding none of
main's credentials. Set `WACTORZ_MODEL_CODE_NODE=sandbox` in `.env` and add the
profile:

```bash
docker compose --profile python --profile sandbox up -d
```

`sandbox-env`, a one-shot, writes the node what `/deploy` writes any node: its
name, the broker, its signing key and broker account, and the CA with TLS. It
writes them into a volume that only `wactorz-sandbox` mounts. It runs again on
every `docker compose up`, so after the broker's CA or the node's account
changes, `docker compose --profile python --profile sandbox up -d` hands the
node the new ones. The sandbox
container mounts neither `.env` nor `./state`, and its network has the broker on
it and nothing else, so main's API and Home Assistant are out of its reach. The
packages its agents install go into its own volume. It is limited by
`WACTORZ_SANDBOX_MEM_LIMIT` (default `2g`) and `WACTORZ_SANDBOX_PIDS_LIMIT`
(`1024`). See [a node on main's own machine](remote-nodes.md#a-node-on-mains-own-machine)
for what goes there and what stays on main.

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
| `WACTORZ_DOCUMENT_DIRS` | your Documents folder | The folders an agent that converts files (the doc-to-pptx agent) may read from and write to, separated like `PATH`. Anything under `WACTORZ_STATE_DIR` stays out of reach whatever is named here. See [Doc → PPTX agent](catalogue-doc-to-pptx.md) |
| `WACTORZ_PIP_ALLOW` | — | Packages an agent may have installed without the operator being asked, separated by commas or spaces, in addition to the built-in list. Anything else is asked about on the dashboard first; `/packages` shows the whole list. See [the packages an agent installs](remote-nodes.md#the-packages-an-agent-installs) |
| `WACTORZ_PIP_INDEX_URL` | — | The package index every install uses, such as a private mirror. Unset, pip uses PyPI |
| `WACTORZ_MODEL_CODE_NODE` | — | The node code the model writes runs on when its spawn names none, usually `sandbox` (see [a node on main's own machine](remote-nodes.md#a-node-on-mains-own-machine)). Unset, such code runs on main |
| `WACTORZ_MODEL_CODE_ON_MAIN` | `ask` | With `WACTORZ_MODEL_CODE_NODE` set, what happens to a spawn the model wrote that names main: `ask` on the dashboard, `allow`, or `never` |
| `WACTORZ_AGENTS` | — | Agents this deployment brings, as `package.module:attr` targets (an `Actor` subclass or a function declared with `@wactorz.agent`), comma separated. Supervised at startup beside the built-ins; see [Bringing your own agents](agents.md#bringing-your-own-agents) |
| `WACTORZ_PIPELINES` | — | Pipelines this deployment brings, as `package.module:attr` targets of what `wactorz.pipeline()` returned, comma separated; their steps, schedule and rules are supervised at startup |
| `WACTORZ_HA_AGENTS` | `auto` | Whether the Home Assistant agents start: `auto` starts them when `HA_URL` and `HA_TOKEN` are set, `on` and `off` decide outright |
| `WACTORZ_MINIMAL` | `0` | Start only the monitor, the dashboard and the agents this deployment brings: no orchestrator, catalogue or installer, so no model is needed. Same as `wactorz --minimal` |
| `WACTORZ_ORCHESTRATOR` | — | What answers chat, as one `package.module:attr` target: an object implementing `wactorz.Orchestrator`, or a class or factory called with the actor registry to build one. In place of main (the default when main runs) and of the model-free orchestrator of the minimal profile; a target that cannot be loaded or is not an orchestrator refuses to start. See *Your own orchestrator* in the library guide |
| `WACTORZ_TZ` | _(unset)_ | Override the timezone used in agents' date/time context (e.g. `Europe/Athens`). Precedence: a user's `pref_timezone` fact > `WACTORZ_TZ` > standard `TZ` > host local zone. Blank or unknown values fall through to the next candidate |
| `WACTORZ_LOG_FORMAT` | `text` | `json` writes each log record as one JSON object on one line (JSON Lines), to the console and to `wactorz.log`, for a collector that parses logs: fields `ts` (UTC), `level`, `logger`, `message`, and `exception` holding the whole traceback. Redaction applies as in text. The dashboard's log view is unaffected. A node reads the setting from its own environment |
| `WACTORZ_RETENTION_CHAT_DAYS` | `365` | Days chat history is kept; `0` keeps it for ever. An attached file goes with the last message that refers to it, or a day after upload if it was never sent |
| `WACTORZ_RETENTION_TIMESERIES_DAYS` | `365` | Days sensor readings, detections, Home Assistant state changes and actuations are kept; `0` keeps them for ever. The time-series collector agent's own `retention_days` applies too, and the shorter window holds |
| `WACTORZ_RETENTION_OUTBOX_DAYS` | `7` | Days an MQTT message the broker never accepted stays in the outbox; `0` keeps it until delivered. Once expired it is not retried after a restart, and the log names its topic. A command — a non-retained message under `nodes/` or `agents/by-name/`, such as a spawn, a stop or a task for an agent — expires after 10 minutes whatever this says, since replaying one later would undo or repeat what has happened since; a node's retained `desired_state` follows this setting |
| `WACTORZ_RETENTION_METRICS_DAYS` | `7` | Days the per-minute metrics history of every agent and node is kept (`/api/history/...`); `0` keeps it for ever. A metrics reset clears it |
| `WACTORZ_MIGRATION_MAX_STATE_BYTES` | `8388608` | Largest agent state a `/migrate` ships, as JSON; a larger one is refused before the agent stops, and `--force` does not override it. Set on main, which sends it to the node it asks; `0` sets no limit |
| `WACTORZ_MIGRATION_MAX_BLOB_BYTES` | `67108864` | Largest total of an agent's blobs (models, arrays, bytes) a `/migrate` carries beside its state, and the largest blob a machine accepts from another; over it, the agent stays where it was. Read on main and on each node; `0` sets no limit |
| `WACTORZ_ARTIFACT_MAX_BYTES` | `268435456` | Largest artifact (a model kept with `wactorz-artifacts`) main sends a node and a node accepts. A spawn naming a larger one is refused before it is sent. Read on main and on each node; `0` sets no limit |
| `WACTORZ_NODE_ARTIFACT_CACHE_BYTES` | `1073741824` | How much of the artifacts it has fetched a node keeps. Past it, those no agent on the node names are deleted, the least recently used first; one an agent names is kept whatever the total. Read on each node; `0` keeps everything |
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

## Backing up and moving an install

`wactorz-state` writes a state directory to one archive, and puts one back:

```bash
wactorz-state export                       # wactorz-state-<date>-<time>.tar.gz, here
wactorz-state export backup.tar.gz --no-secrets
wactorz-state import backup.tar.gz         # into WACTORZ_STATE_DIR, or ./state
wactorz-state import backup.tar.gz --replace
```

Both read `WACTORZ_STATE_DIR` as the server does; `--state-dir` names another directory.

- **What it holds:** the database, each agent's state file and blobs, uploads, the models kept with `wactorz-artifacts`, the install's id, the nodes' SSH host keys and, unless `--no-secrets` is given, the install's keys: `node_signing.key`, which every node checks main's messages against, and the broker's TLS keys in `mqtt_tls/`. With them, a restored install carries on as before; without them it makes new ones, and every node has to be deployed again. An archive with keys is written readable by its owner only, and is to be kept like the keys.
- **What it leaves out:** logs, the MQTT outbox (what it holds would be stale by the time it was restored), dashboard sign-in sessions, files part way through being written or received, files moved aside as unreadable, and directory artifacts as unpacked for agents to read, which are unpacked again from the archive kept beside them.
- **Export while it runs.** The database is copied with SQLite's own backup, consistent however busy it is, and every other file is replaced in one step whenever it is written. What was persisted in the last second may be missing.
- **Import with it stopped.** A running server or node holds a lock on its state directory, and an import refuses while it is held; so does a second server started on the same directory by mistake. Import refuses a directory with anything in it unless `--replace` is given, which keeps what was there beside it as `<directory>.before-import-<date>-<time>`.
- **An archive is code.** Agents' state files are pickles, unpickled when the system starts: importing an archive runs whatever its maker put in it. Import only archives you made. Import checks that an archive holds exactly the files its manifest lists, each matching its SHA-256, at a path inside the directory, which catches a damaged archive, not a forged one.

A node's state is moved the same way, on the node: `wactorz-state export --state-dir ~/wactorz/state`, and an import there with the node stopped. The Home Assistant add-on's state is in `/data/state`, which Home Assistant's own backups already include.

## Models kept on main

`wactorz-artifacts` keeps models and other large files that agents load, each under a name and a version:

```bash
wactorz-artifacts add detector.onnx imu-detector --meta dataset=imu-2026 --meta auc=0.93
wactorz-artifacts add yolo-model/ yolo                       # a directory, kept as one archive
wactorz-artifacts add model.pt imu-detector --format state_dict
wactorz-artifacts list                                       # every name and version
wactorz-artifacts show imu-detector:latest                   # one version, as JSON
wactorz-artifacts remove imu-detector:1                      # one version
wactorz-artifacts remove imu-detector --every-version
wactorz-artifacts verify                                     # stored files still match their hashes
wactorz-artifacts activate imu-detector:3 --reason "auc 0.95"  # the version agents serve
wactorz-artifacts shadow imu-detector:4                      # one on trial beside it
wactorz-artifacts promote imu-detector                       # the shadow becomes active
wactorz-artifacts clear shadow imu-detector                  # unset a pointer
```

- **A name and a version name one file.** Each `add` of a name is its next version. A reference is `NAME:3` for the third version, `NAME:latest` for the newest, `NAME:active` or `NAME:shadow` for the version a pointer names, or `NAME` alone for the active version, which is the newest until one is marked active; each resolves to the file's SHA-256, so what a machine fetches can be checked against it. The same bytes, under any names, are kept once.
- **Two pointers say which version is served and which is on trial.** `activate NAME:V` marks the version agents get when they name the model without a version; a new version stored after that changes nothing until it is activated, and going back is activating the earlier one again. `shadow NAME:V` marks one to run beside it, on the same input, acted on by nobody; `promote NAME` makes the shadow active and clears `shadow`. A move records who made it and `--reason` why, and `list` and `show` say where the pointers stand. Pointing `shadow` at the active version is refused, as is moving a pointer to where it is; a version a pointer names cannot be removed until the pointer is moved; `remove NAME --every-version` takes the pointers with the name. An agent that named the model, or `NAME:active`, follows the pointer: restarted after a move, it is pinned to where `active` stands then; one that named a version, or `latest`, keeps what it ran. `docs/design/model-pointers.md` records the decision, and the rest of the model loop builds on it.
- **Every machine hears a move.** Main publishes where each pointer stands, retained, on `models/NAME/active` and `models/NAME/shadow`: the pointer as the store keeps it, or an empty payload when it is unset. Everything is published when main starts, and a move made beside it by `wactorz-artifacts` reaches the broker at main's next look at the index, a few seconds later; one made in chat is published at once. Each move found is a line on the activity feed. The topics are main's to publish: a pointer is moved through the store or through main, never by writing there.
- **The format comes from the file's ending**: `.onnx`, `.safetensors`, `.npz`, `.npy`, `.gguf`, `.tflite`, `.skops`. `--format` names it where the ending does not say, as for a `.pt`, which may hold a `state_dict` or a whole module; `raw` keeps bytes as they are. A directory is kept as one archive of it, written the same way on every machine, so the same directory has the same hash wherever it is added; a link inside it is refused.
- **Formats that run code when they load are refused** unless `--allow-code` is given: a pickle (`.pkl`), a joblib file and a whole torch module (`--format torch`). One stored anyway is marked as running code.
- **Where:** `<state>/..artifacts/`, beside the rest of the state, in what `wactorz-state export` writes. `--state-dir` names another state directory. Adding and removing work while the server runs.
- **In chat.** `/artifacts` lists what is kept, and `/artifacts <name>` every version of one. `/artifacts activate <name:version> [reason]`, `/artifacts shadow <name:version> [reason]`, `/artifacts promote <name> [reason]` and `/artifacts clear active|shadow <name>` move the pointers as the command does, recorded as made from chat; a social channel may list but not move. Main's model is given the same list on each turn, with the active and shadow version of each model, and so is the planner when a request becomes a pipeline, so asking for an agent that uses a model ("flag IMU readings far from normal with the imu-detector model") gets one that names it, and the one being served. Main's model has no way to move a pointer: serving a model is the operator's decision, or a deployed program's.
- **Which store.** Every `wactorz-artifacts` command says which store it used, and `add` warns when it had to make a new state directory: the default is `./state` in the folder it runs from, which is Wactorz's own only when run from where Wactorz runs. Pass `--state-dir`, or set `WACTORZ_STATE_DIR` for both.
- **In an agent.** A generated or catalogue agent names artifacts in its spawn config, `"artifacts": {"model": "imu-detector:3"}`, and its program opens one with `agent.artifact_path("model")`: the file, or for a directory the directory it unpacks to, once, in `<state>/..unpacked-artifacts/` (left out of an export, and unpacked again when needed). A spawn on main that names an artifact that is not stored is refused.
- **On a node.** Main pins each artifact to the version and the bytes it means when the spawn goes out — `imu-detector` alone means the newest then — and keeps that pinned form, so the agent restarted or moved later runs the same model. The node keeps what it fetches in `<state>/..artifact-cache/` across restarts, asks main only for what it lacks, and starts the agent once every file is there and matches its SHA-256; one that does not arrive leaves the agent unstarted, and a migration is put back where it was. Main sends a node only the artifacts an agent placed on it names. A spawn naming an artifact that is not stored, or one over `WACTORZ_ARTIFACT_MAX_BYTES`, is refused before anything is sent.

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
