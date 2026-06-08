# Installation

Wactorz requires Python 3.10+ and a running MQTT broker (Mosquitto). The `wactorz` command starts the actor system and the web dashboard — start Mosquitto first with `docker compose up -d`.

---

## Install

#### From PyPI

```bash
pip install wactorz[all]
```

#### Latest from GitHub

```bash
pip install "wactorz[all] @ git+https://github.com/waldiez/wactorz.git"
```

#### From source (editable)

```bash
git clone https://github.com/waldiez/wactorz.git
cd wactorz
pip install -e ".[all]"
```


---

## Optional dependencies

The `[all]` extra installs everything except the ML stack (heavy torch dependency). Install individual extras as needed:

| Extra | Installs | Needed for |
|---|---|---|
| `wactorz[anthropic]` | `anthropic` | `--llm anthropic` (default) |
| `wactorz[openai]` | `openai` | `--llm openai` |
| `wactorz[google]` | `google-genai` | `--llm gemini` |
| `wactorz[discord]` | `discord.py` | `--interface discord` |
| `wactorz[whatsapp]` | `twilio` | `--interface whatsapp` |
| `wactorz[mcp]` | `mcp` | MCP-compatible clients |
| `wactorz[otel]` | OpenTelemetry SDK/exporter | OTLP tracing |
| `wactorz[influx]` | `influxdb-client` | InfluxDB time-series export |
| `wactorz[tts]` | `edge-tts` | text-to-speech support |
| `wactorz[ml]` | `ultralytics`, `torch`, `numpy` | webcam detection pipelines |
| `wactorz[all]` | all of the above except `ml` | recommended starting point |

> **Tip:** You only need to install the dep for the provider you actually use. If you plan to switch providers, install `wactorz[all]` once and set the active provider via `--llm` flag or `LLM_PROVIDER` in `.env`.

---

## Quick start

```bash
# 1. Create a .env file with your LLM key (see Configuration below)
cp .env.template .env
# edit .env and set LLM_API_KEY (or whichever provider you use)

# 2. Get/run MQTT broker (if you don't have already) and open ports 1883, 9001
docker run -d --name mosquitto -p 1883:1883 -p 9001:9001 eclipse-mosquitto

# 3. Start Wactorz
wactorz

# 4. Open the web dashboard
# http://localhost:8888
```

That's it. The CLI interface starts in your terminal and the web dashboard opens at `localhost:8888`. Both can be used simultaneously.

#### Switch interface or provider

```bash
# Use Gemini instead of Claude
wactorz --llm gemini --gemini-model gemini-2.5-flash

# Discord bot
wactorz --interface discord --discord-token $DISCORD_BOT_TOKEN

# Hot-reload during development (restarts on source file changes)
wactorz --reload
```

---

## Configuration

Wactorz reads configuration from a `.env` file in the working directory. All values can also be passed as environment variables.

#### Core

```env
# LLM provider — anthropic | openai | ollama | nim | gemini
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6

# API keys — set whichever provider you use
LLM_API_KEY=sk-ant-...
```

#### Home Assistant

```env
HA_URL=http://homeassistant.local:8123
HA_TOKEN=eyJ...              # Long-lived access token from HA profile page
HA_STATE_BRIDGE_PER_ENTITY=0 # 0 = flat topic (default), 1 = per-entity topics
```

#### Interfaces

```env
# Discord
DISCORD_BOT_TOKEN=MTI4...

# WhatsApp (Twilio)
TWILIO_ACCOUNT_SID=ACxxx...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886

# Telegram
TELEGRAM_BOT_TOKEN=1234567890:AAF...

# REST API auth (optional)
API_KEY=my-secret-key

# MCP server (optional)
WACTORZ_URL=http://localhost:8000
WACTORZ_API_KEY=              # optional; mirrors API_KEY for REST auth
```

#### MQTT broker

```env
# Only needed if using an external broker instead of the embedded one
MQTT_HOST=localhost
MQTT_PORT=1883
```

#### Web dashboard

```env
WS_PORT=8888   # dashboard port, default 8888
```

---

## Running Mosquitto via Docker

```bash
docker run -d --name mosquitto -p 1883:1883 -p 9001:9001 eclipse-mosquitto
```

> **Note (Docker Desktop on Windows/Mac):** When services run inside Docker and need to reach the broker on the host, use `host.docker.internal` as the broker hostname instead of `localhost`.

---

## Web UI

The web dashboard at `http://localhost:8888` starts automatically with every `wactorz` invocation. It provides a real-time view of the running system:

- **Agent grid** — live heartbeat status for every registered actor
- **Log stream** — real-time output from all agents via WebSocket
- **Chat** — full conversation interface, equivalent to the CLI
- **Docs** — this documentation at `/docs/`

```bash
# Change the dashboard port
wactorz --monitor-port 9000

# Disable the dashboard entirely
wactorz --no-monitor
```

> **Browser cache:** If you update doc files and still see old content, do a hard refresh: **Ctrl+Shift+R** (Windows/Linux) or **Cmd+Shift+R** (Mac).

---

## MCP

Wactorz includes an optional Model Context Protocol server for local MCP clients. Install the extra, start Wactorz's REST interface, then point your MCP client at the stdio server.

```bash
pip install -e ".[mcp]"

# Terminal 1
wactorz --interface rest --port 8000

# Terminal 2: verify registered tools
python -c "import asyncio, wactorz.interfaces.mcp_server as s; print([t.name for t in asyncio.run(s.mcp.list_tools())])"
```

The registered tools are:

```text
ask_wactorz, ask_agent, list_agents, list_capabilities,
stop_agent, pause_agent, resume_agent,
ha_list_entities, ha_get_state, ha_call_service
```

Test with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python -m wactorz.interfaces.mcp_server
```

Use `list_agents`, `ask_wactorz` with `/agents`, and `wactorz://config` as the first read-only checks. Home Assistant tools require `HA_URL` and `HA_TOKEN`; service calls can change real devices, so test read-only tools before `ha_call_service`.

MCP client config:

```json
{
  "mcpServers": {
    "wactorz": {
      "command": "python",
      "args": ["-m", "wactorz.interfaces.mcp_server"],
      "env": {
        "WACTORZ_URL": "http://localhost:8000"
      }
    }
  }
}
```

ChatGPT custom connectors require a remote HTTP/SSE MCP server; this Wactorz MCP entrypoint is currently a local stdio server intended for desktop/editor MCP clients.

---

## Docker

Wactorz ships with a `Dockerfile` and Docker Compose files for running the full stack in containers. Docker is the recommended approach for production deployments and reproducible dev environments.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) or Docker Engine (Linux)
- A `.env` file copied from `.env.template` with your secrets filled in

```bash
cp .env.template .env
# edit .env and set LLM_API_KEY, FUSEKI_PASSWORD, etc.
```

### Production stack (profiles)

The main `compose.yaml` uses profiles so you only start what you need:

```bash
# Python agents + MQTT + Fuseki (recommended)
docker compose --profile python-full up -d

# Python agents + MQTT only (no Fuseki)
docker compose --profile python up -d

# MQTT broker only (default)
docker compose up -d

# Rust server + nginx dashboard
docker compose --profile rust up -d

# Everything (Rust + Fuseki + Home Assistant)
docker compose --profile full up -d
```

| Profile | Services | Ports |
|---|---|---|
| *(default)* | mosquitto | :1883, :9001 |
| `python` | + wactorz-python | + :8000, :8888 |
| `python-full` | + wactorz-python, fuseki | + :8000, :8888, :3030 |
| `rust` | + wactorz-server, dashboard | + :8080, :8081, :80 |
| `full` | + rust, fuseki, homeassistant | + :8080, :8081, :80, :3030, :8123 |

Once running:

| Service | URL | Credentials |
|---|---|---|
| Web UI | http://localhost:8888 | — |
| REST API | http://localhost:8000 | — |
| Fuseki | http://localhost:3030 | admin / `FUSEKI_PASSWORD` from `.env` |

### Stopping and teardown

```bash
# Stop services
docker compose --profile python-full down

# Stop and remove all volumes and persisted data
docker compose --profile python-full down -v
```

### Rebuilding after code changes or a fresh start

```bash
# Rebuild and restart just the Python app
docker compose --profile python-full up -d --build wactorz-python

# Full teardown and clean rebuild
docker compose --profile python-full down -v
docker compose --profile python-full up -d --build
```

### Development stack

For local development, `compose.dev.yaml` starts MQTT + Fuseki + the Python app together with a single command:

```bash
# Start everything
docker compose -f compose.dev.yaml up -d --build

# View logs
docker compose -f compose.dev.yaml logs -f wactorz

# Stop everything
docker compose -f compose.dev.yaml down
```

Services started:

| Container | Port | Description |
|---|---|---|
| `wactorz-app` | :8000, :8888 | Python agent system (REST API + Web UI) |
| `wactorz-fuseki` | :3030 | Apache Jena Fuseki (knowledge graph) |
| `wactorz-dev-mosquitto` | :1883, :9001 | MQTT broker (TCP + WebSocket) |

### Environment variables in Docker

Your `.env` file is loaded automatically via `env_file`. The compose files override a few values so services can reach each other by container name instead of `localhost`:

```env
# These are set automatically by compose — do not override in .env
MQTT_HOST=mosquitto         # container name, not localhost
FUSEKI_URL=http://fuseki:3030
```

> **Warning (Windows line endings):** If Fuseki fails with `exec /entrypoint.sh: no such file or directory`, the shell script has Windows CRLF line endings. Fix with:
> ```bash
> docker run --rm -v "$PWD/config/fuseki-container:/work" alpine sh -c "sed -i 's/\r//' /work/entrypoint.sh"
> ```
> Then rebuild: `docker compose --profile python-full up -d --build fuseki`

> **Fuseki admin UI:** After starting Fuseki, the admin UI is at `http://localhost:3030`. Default credentials: **admin / admin** (set via `FUSEKI_ADMIN_PASSWORD` in your `.env`).

---

## Project structure

```
wactorz/                         ← repo root
├── wactorz/                     ← Python package
│   ├── cli.py                   ← entry point (wactorz command)
│   ├── config.py                ← .env loading, CONFIG object
│   ├── monitor_server.py        ← web dashboard (aiohttp)
│   ├── core/
│   │   ├── actor.py             ← Actor base class, Supervisor, persistence
│   │   └── registry.py          ← ActorSystem, ActorRegistry, MQTT publisher
│   │   ├── persistence.py       ← Actor storage to SQlite
│   │   └── migrations.py        ← Migration to nodes
│   │   ├── topic_bus.py         ← Reactive Pub/Sub Coordination Layer
│   ├── agents/
│   │   ├── main_actor.py        ← LLM orchestrator
│   │   ├── llm_agent.py         ← LLM base + all providers
│   │   ├── home_assistant_agent.py
│   │   ├── prompts/
│   │   │   └── home_assistant_prompts.py  ← HA LLM system prompts
│   │   ├── home_assistant_state_bridge_agent.py
│   │   ├── home_assistant_map_agent.py
│   │   ├── monitor_agent.py
│   │   ├── installer_agent.py
│   │   └── io_agent.py
│   ├── catalogue_agents/        ← pre-built DynamicAgent recipes
│   ├── interfaces/
│   │   ├── chat_interfaces.py   ← CLI, REST, Discord, WhatsApp, Telegram
│   │   └── mcp_server.py        ← MCP tools/resources for compatible clients
├── static/                      ← source docs + frontend SPA
├── state/                       ← agent persistence (created at runtime)
├── Dockerfile                   ← Python app container
├── .dockerignore
├── compose.yaml                 ← production stack (profiles)
├── compose.dev.yaml             ← development stack
├── .env                         ← your config (gitignored)
├── .env.template                ← annotated config template
└── pyproject.toml
```

---

## Development

### Editable install

```bash
git clone https://github.com/waldiez/wactorz.git
cd wactorz
pip install -e ".[all]"

# Start with hot-reload (restarts on .py/.yaml file changes)
wactorz --reload
```

### Adding a catalog recipe

```bash
# 1. Create the recipe file
#    Must export AGENT_CODE = r'''...'''
touch wactorz/catalogue_agents/my_agent.py

# 2. Register it in catalog_agent.py → _build_catalog()
#    code = _load_recipe("my_agent.py")
#    if code:
#        catalog["my-agent"] = { ...spawn config..., "code": code }

# 3. Restart wactorz — recipe is immediately available
@catalog list
@catalog spawn my-agent
```

### Running tests

```bash
pip install -e ".[dev]"
python -m unittest discover -s tests -p 'test_*.py'
```

---

## Debugging

#### Watch all MQTT traffic

```bash
mosquitto_sub -h localhost -p 1883 -t '#' -v
```

#### Watch a specific agent's logs

```bash
mosquitto_sub -h localhost -t 'agents/+/logs' -v
```

#### Check agent status directly

```
@monitor {"action": "status"}
@catalog list
```

#### Read persistence state

The spawn registry, user facts, and pipeline rules now live in SQLite (`state/wactorz.db`), routed automatically by `PersistenceAPI`. Read them with:

```python
python3 -c "
import sqlite3, json
conn = sqlite3.connect('state/wactorz.db')
row = conn.execute(
    \"SELECT value FROM kv_store WHERE agent='main' AND key='_spawned_agents'\"
).fetchone()
spawned = json.loads(row[0]) if row else {}
print('Spawn registry:', list(spawned.keys()))

row = conn.execute(
    \"SELECT value FROM kv_store WHERE agent='main' AND key='_user_facts'\"
).fetchone()
print('User facts:', json.loads(row[0]) if row else {})
"
```

The `spawn_registry` table also holds spawn configs in a structured form:

```bash
sqlite3 state/wactorz.db "SELECT name, node FROM spawn_registry;"
```

#### Remove a stuck agent from the spawn registry

```python
python3 -c "
import sqlite3, json
conn = sqlite3.connect('state/wactorz.db')
row = conn.execute(
    \"SELECT value FROM kv_store WHERE agent='main' AND key='_spawned_agents'\"
).fetchone()
spawned = json.loads(row[0]) if row else {}
spawned.pop('my-stuck-agent', None)
conn.execute(
    \"UPDATE kv_store SET value=? WHERE agent='main' AND key='_spawned_agents'\",
    (json.dumps(spawned),),
)
conn.execute(\"DELETE FROM spawn_registry WHERE name='my-stuck-agent'\")
conn.commit()
print('Done. Remaining:', list(spawned.keys()))
"
```
