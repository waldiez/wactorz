# AgentFlow

**Actor-Model Multi-Agent Framework**  
*Technical Reference & Developer Guide*

---

## Table of Contents

1. [What is AgentFlow?](#1-what-is-agentflow)
2. [Architecture](#2-architecture)
3. [Agent Types](#3-agent-types)
4. [Spawning Agents at Runtime](#4-spawning-agents-at-runtime)
5. [Agent-to-Agent Communication](#5-agent-to-agent-communication)
6. [Health Monitoring & Error Recovery](#6-health-monitoring--error-recovery)
7. [Persistence & State](#7-persistence--state)
8. [LLM Cost Tracking](#8-llm-cost-tracking)
9. [Interfaces](#9-interfaces)
10. [MQTT Topic Reference](#10-mqtt-topic-reference)
11. [Built-in Specialist Agents](#11-built-in-specialist-agents)
12. [Remote Nodes & Edge Deployment](#12-remote-nodes--edge-deployment)
13. [Installation & Configuration](#13-installation--configuration)
14. [Troubleshooting](#14-troubleshooting)
15. [File Structure](#appendix-file-structure)

---

## 1. What is AgentFlow?

AgentFlow is an asynchronous, actor-model multi-agent framework built from scratch in Python. It allows an LLM orchestrator ("main") to spawn, coordinate, monitor, and retire live software agents at runtime — without any code restart or predefined agent types.

The core idea is simple: you talk to the system in natural language. The LLM writes Python code, wraps it in a `<spawn>` block, and a new agent appears — running in its own async actor, connected to all other agents via MQTT and direct actor messaging, and persisting its state to disk automatically.

AgentFlow was born out of the need for a framework that could operate on real-world IoT data streams at the edge — something existing agent frameworks (LangGraph, CrewAI, AutoGen) were not designed for. It is lightweight enough to run on modest hardware, offline-capable, and fully async.

### Design Principles

- **Everything is an Actor** — agents communicate via messages, not function calls
- **Agents are spawned at runtime** — no hardcoded types, no restart required
- **MQTT is the nervous system** — all events, heartbeats, and results flow through topics
- **Persistence is automatic** — every agent survives a crash and restores its state
- **The LLM is the orchestrator** — it decides what agents to create and how to wire them
- **Errors are first-class** — structured error events trigger real recovery actions

---

## 2. Architecture

### The Actor Model

Each agent is an Actor: an independent unit with its own async message loop, mailbox (`asyncio.Queue`), and lifecycle (`CREATED → RUNNING → PAUSED → STOPPED / FAILED`). Actors never share memory. They communicate by sending typed `Message` objects to each other via the `ActorRegistry`, which maps actor IDs to actor instances.

```
Message flow:

  Actor A                Registry              Actor B
  ───────               ──────────             ───────
  send(B_id, TASK, {…}) ──────────────────►  mailbox.put(msg)
                                              message_loop picks it up
                                              handle_message(msg) fires
                        ◄─────────────────── send(A_id, RESULT, {…})
  handle_message fires
  future.set_result(…)
```

### Core Components

| File | Layer | Role |
|------|-------|------|
| `core/actor.py` | Core | Base Actor class — mailbox, lifecycle, heartbeat, spawn, send, persist/recall |
| `core/registry.py` | Core | ActorSystem & ActorRegistry — actor registration, message routing, broadcast |
| `agents/main_actor.py` | Agent | The LLM orchestrator — processes user input, spawns agents, routes requests |
| `agents/monitor_agent.py` | Agent | Health watcher — detects crashes, fires recovery actions, notifies user |
| `agents/llm_agent.py` | Agent | Base LLM agent with conversation history, cost tracking, streaming, and 4 providers |
| `agents/dynamic_agent.py` | Agent | Runtime-generated agents — executes LLM-written Python code in a sandboxed namespace |
| `agents/planner_agent.py` | Agent | Multi-step task planner — decomposes tasks, fans out to workers, synthesizes results |
| `agents/installer_agent.py` | Agent | Package manager — installs pip packages locally and on remote nodes via SSH |
| `agents/manual_agent.py` | Agent | PDF specialist — 3-layer search strategy to find and extract manual content |
| `agents/home_assistant_agent.py` | Agent | Unified HA agent — hardware recommendations and automation CRUD via HA REST API |
| `interfaces/chat_interfaces.py` | I/O | CLI (streaming), REST, Discord, WhatsApp — all call `process_user_input[_stream]` |
| `monitor_server.py` | I/O | MQTT→WebSocket bridge + static server + MQTT WS proxy; serves the built frontend at `/` and `monitor.html` at `/ws` |
| `frontend/` | I/O | Primary dashboard SPA — Babylon.js frontend served as `index.html` |

---

## 3. Agent Types

### LLMAgent (base)

All LLM-backed agents inherit from `LLMAgent`, which inherits from `Actor`. It manages conversation history (persisted to disk), tracks token usage and cost across 4 providers, and supports both blocking and streaming responses.

**Supported LLM providers:**

| Provider | Key | Notes |
|----------|-----|-------|
| Anthropic Claude | `ANTHROPIC_API_KEY` | Default |
| OpenAI | `OPENAI_API_KEY` | `--llm openai` |
| Ollama | *(none)* | Local models, `--llm ollama --ollama-model llama3` |
| NVIDIA NIM | `NIM_API_KEY` | Free tier 1000 req/month, `--llm nim --nim-model meta/llama-3.3-70b-instruct` |

### DynamicAgent

The heart of AgentFlow. When the LLM writes a spawn block, a `DynamicAgent` is created with that code compiled into its namespace. Three optional async functions can be defined:

```python
async def setup(agent):
    # Runs once at startup
    await agent.log('ready')

async def process(agent):
    # Runs in a loop every poll_interval seconds
    data = read_sensor()
    await agent.publish('sensors/temp', data)

async def handle_task(agent, payload):
    # Runs on demand when a task arrives
    city = payload.get('city', 'Athens')
    return {'temp': fetch_weather(city)}
```

**The `agent` API (available inside all three functions):**

| Method | Description |
|--------|-------------|
| `agent.log(msg)` | Publish a log event |
| `agent.publish(topic, data)` | Publish to an MQTT topic |
| `agent.persist(key, value)` / `agent.recall(key)` | Durable key-value state |
| `agent.llm.chat(prompt)` | Call the LLM |
| `agent.send_to(name, payload)` | Send a task to another agent by name |
| `agent.delegate(name, payload)` | Same, with cleaner syntax |
| `agent.send_to_many(tasks)` | Fan-out to multiple agents in parallel |
| `agent.agents()` | List all currently running agents |

### MainActor

The user-facing orchestrator. Every message you type is processed by main, which:

1. Checks if it is a Home Assistant request (LLM classifier)
2. Checks if it needs multi-agent planning (complexity heuristic or explicit `coordinate:` prefix)
3. Drains any pending monitor notifications and prepends them to the response
4. Falls back to its own LLM conversation for everything else
5. Parses `<spawn>` blocks in the LLM output and creates agents automatically

### PlannerAgent

Spawned on-demand when a task is too complex for a single agent. Its pipeline:

1. Check plan cache — reuse plan structure if the task is similar to a recent one (24h TTL)
2. Discover all running worker agents
3. Ask the LLM to decompose the task into a dependency graph of steps
4. Spawn any missing agents declared in the plan (with `spawn_config`)
5. Execute parallel steps with `asyncio.gather`, inject context into dependent steps
6. Synthesize all results into a clean user-facing answer
7. Cache the plan to disk, self-terminate after 2 seconds

**Trigger the planner explicitly or automatically:**

```
coordinate: get the weather in Athens and search for AI news, then combine them
plan: load the Philips manual and answer the cleaning question
@planner   any complex multi-step task
```

The planner also generates missing agents on the fly. If the LLM decides a step needs an agent that doesn't exist yet, it includes a `spawn_config` in the plan — the planner spawns it, registers it with main's spawn registry (so it survives restarts), and proceeds.

---

## 4. Spawning Agents at Runtime

Simply describe what you want in the chat. The LLM will write the code and wrap it in a `<spawn>` block. You never need to write code yourself.

### The Spawn Block

```json
<spawn>
{
  "name": "weather-agent",
  "type": "dynamic",
  "description": "Fetches live weather from Open-Meteo",
  "install": ["httpx"],
  "poll_interval": 3600,
  "code": "
    async def setup(agent):
        await agent.log('Weather agent ready')

    async def handle_task(agent, payload):
        import httpx
        city = payload.get('city', 'Athens')
        async with httpx.AsyncClient() as c:
            r = await c.get(f'https://wttr.in/{city}?format=j1')
        return r.json()['current_condition'][0]
  "
}
</spawn>
```

### Spawn Options

| Field | Description |
|-------|-------------|
| `name` | Unique agent name. Use `"replace": true` to hot-swap a running agent |
| `type` | `"dynamic"` (runtime code), `"llm"` (pure conversation), `"manual"` (PDF search) |
| `node` | Remote node name to spawn on (e.g. `"rpi-kitchen"`). Omit to run locally |
| `install` | List of pip packages to install before spawning. Fast-path skips if already importable |
| `poll_interval` | Seconds between `process()` calls. Use `3600` for infrequent background tasks |
| `replace` | If `true`, stops the existing agent with this name before spawning the new one |
| `code` | The Python source. May define `setup()`, `process()`, and/or `handle_task()` |
| `system_prompt` | For `type: "llm"` agents — the LLM's persona and instructions |
| `description` | Human-readable description shown in the dashboard and used by the planner |

Agents with packages in `"install"` are spawned in the background. A fast-path checks whether packages are already importable first — if they are, spawning is instant (no installer round-trip needed). All spawned agents are saved to the spawn registry and automatically restored on the next startup.

---

## 5. Agent-to-Agent Communication

Agents can talk to each other directly — no LLM involved, pure actor messaging with futures for synchronous results.

### From inside a DynamicAgent

```python
async def handle_task(agent, payload):
    # Ask another agent and wait for the result
    weather = await agent.delegate('weather-agent', {'city': 'Athens'})

    # Fan-out to multiple agents in parallel
    results = await agent.send_to_many([
        ('weather-agent', {'city': 'Athens'}),
        ('news-agent',    {'query': 'AI today'}),
    ])

    # List all running agents
    workers = agent.agents()
    # [{'name': 'weather-agent', 'type': 'DynamicAgent', ...}, ...]
```

### Addressing Agents in Chat

```
@agent-name  your message here    — route directly to that agent
@main        your message here    — route to the main orchestrator
@planner     your complex task    — explicitly trigger the planner
```

---

## 6. Health Monitoring & Error Recovery

AgentFlow has a four-layer error handling system. Errors are first-class events, not just log lines.

### Layer 1 — DynamicAgent: Structured Error Events

Every error site (`compile`, `setup`, `process`, `handle_task`) publishes a structured error event with `phase`, `severity`, `traceback`, and `consecutive` error count. After 3 consecutive errors the agent is marked `degraded`. Exponential backoff kicks in for `process()` errors (2s → 4s → 8s → max 30s). The error count resets after any successful operation.

### Layer 2 — MonitorAgent: Error Registry & Recovery

The monitor subscribes to error events from all agents and maintains an error registry. Recovery decisions:

| Severity | Action |
|----------|--------|
| `warning` | Log it, let the agent recover on its own |
| `critical` / `degraded` | Attempt restart (up to 3 times) |
| `fatal` (compile/setup) | Do NOT restart — the code is broken. Notify user to fix it |

**Heartbeat liveness:** every actor publishes a heartbeat every 10 seconds. The monitor reads `metrics.last_heartbeat` directly, so even idle agents (installer, manual-agent) are never falsely flagged as unresponsive. Infrastructure agents (monitor, installer, main, code-agent, anomaly-detector, home-assistant-agent) are excluded from user-facing notifications.

### Layer 3 — MainActor: User Notification

Monitor notifications are queued and prepended to the user's next response with severity icons:

- 🔴 **critical** — agent is broken, needs attention
- 🟡 **warning** — agent had issues, monitor is handling it
- ✅ **recovered** — agent is running normally again

### Layer 4 — PlannerAgent: Graceful Fallback

If a worker agent returns an error during a planner step, the planner logs it and falls back to asking main's LLM directly for that step — so the user gets a partial answer rather than a silent failure.

---

## 7. Persistence & State

Every actor has access to a simple key-value persistence API backed by JSON files in the `persist/` directory. State is automatically restored on restart.

```python
# Inside any agent
agent.persist('my_key', {'count': 42, 'data': [...]})   # write
value = agent.recall('my_key', default={})               # read
```

Used internally for:

- Conversation history (`LLMAgent`) — sanitized on every load
- Plan cache (`PlannerAgent`) — 24h TTL, invalidated if required agents are gone
- Loaded PDF content (`ManualAgent`) — avoids re-downloading on repeated questions
- Spawn registry (`MainActor`) — restores all agents on startup

Conversation history is sanitized on every load — any corrupted entries (non-string content, wrong role names) are stripped before the Anthropic API is called. If you encounter a corrupted history from a previous session, run `fix_history.py` once to clean it up.

---

## 8. LLM Cost Tracking

Every LLM call across every agent accumulates token usage into three counters: `total_input_tokens`, `total_output_tokens`, and `total_cost_usd`. These are visible per-agent in the dashboard and via `/cost` in the CLI.

Cost is tracked for all four providers (Anthropic, OpenAI, Ollama free, NIM free/paid). The `HomeAssistantAgent` tracks costs across all 7 of its internal LLM calls: classification, hardware selection, correction retry, automation generation, delete confirmation, edit identification, and edit generation.

---

## 9. Interfaces

### CLI (Streaming)

```bash
python -m agentflow                                              # Anthropic Claude (default)
python -m agentflow --llm openai
python -m agentflow --llm ollama --ollama-model llama3
python -m agentflow --llm nim --nim-model meta/llama-3.3-70b-instruct
```

**CLI commands:**

| Command | Description |
|---------|-------------|
| `/agents` | List all running agents with type and status |
| `/nodes` | List remote nodes with online/offline status and their agents |
| `/deploy <node-name>` | Bootstrap a new remote node via SSH (discovers Pi automatically) |
| `/deploy-pkg <host> <pkg...>` | Install pip packages on a remote node via SSH |
| `/migrate <agent> <node>` | Move a running agent to a different node |
| `/cost` | Show per-agent token usage and cost breakdown |
| `/clear` | Clear the main agent's conversation history |
| `/clear-plans` | Wipe the planner's plan cache |
| `/help` | Show all available commands |
| `@agent-name` | Route your next message directly to a specific agent |

### REST API

Start with `--interface rest` (default port 8080). Send `POST` requests to `/chat` with `{"message": "..."}`. Responses are blocking (non-streaming). Suitable for integration with other services.

### Discord & WhatsApp

Set `DISCORD_TOKEN` or `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_WHATSAPP_FROM` environment variables and start with `--interface discord` or `--interface whatsapp`. The same `process_user_input()` pipeline handles all interfaces.

### Live Dashboard

**Via Vite dev server (development):**
```bash
cd frontend && npm run dev   # → http://localhost:5173
```

**Via `monitor_server.py` (zero-nginx option):**
```bash
cd frontend && npm run build   # produces frontend/dist/
python monitor_server.py       # → http://localhost:8888
# Optionally: --mqtt-ws-port 9001 (default) if Mosquitto WS is on a different port
```
`monitor_server.py` serves the built SPA at `/`, proxies MQTT WebSocket at `/mqtt`, and the legacy `monitor.html` dashboard remains accessible via the `/ws` endpoint it exposes.

**Production** — nginx is the recommended entry point; see the Docker compose files.

---

## 10. MQTT Topic Reference

| Topic | Description |
|-------|-------------|
| `agents/{id}/heartbeat` | Liveness pulse every 10s — name, state, metrics |
| `agents/{id}/logs` | Log events, spawn notifications, user interactions |
| `agents/{id}/errors` | Structured error events with phase, severity, traceback |
| `agents/{id}/alert` | Alert events (heartbeat timeout or error escalation) |
| `agents/{id}/metrics` | Token usage, cost, tasks completed after each LLM call |
| `agents/{id}/completed` | Task completion notification with result preview |
| `agents/by-name/{name}/task` | Address a task to an agent by name (used by remote agents) |
| `system/health` | Global health snapshot every 15s — running/stopped/failed counts |
| `nodes/{name}/spawn` | Spawn a new agent on a remote node |
| `nodes/{name}/stop` | Stop a named agent on a remote node |
| `nodes/{name}/migrate` | Move an agent from this node to another |
| `nodes/{name}/list` | Request list of agents running on a node |
| `nodes/{name}/heartbeat` | Node liveness pulse — agent list, broker, timestamp |
| `nodes/{name}/migrate_result` | Migration success/failure notification |

---

## 11. Built-in Specialist Agents

### ManualAgent — PDF Specialist

Finds and extracts product manuals from the web using a 3-layer search strategy:

1. **Direct URL construction** — for known brands (e.g. Philips), tries manufacturer CDNs directly with a HEAD request
2. **DuckDuckGo search** — with multiple key name fallbacks (`href`, `url`, `link`)
3. **Bing HTML scrape** — parses HTML for PDF links and trusted manual site URLs

PDF content is extracted in memory (`pdfplumber` → `pymupdf` fallback) and stored in the agent's persistence so repeat questions don't require re-downloading.

### HomeAssistantAgent — HA Automation

Connects to your Home Assistant instance (set `HA_URL` and `HA_TOKEN`) and handles five intents, classified by a cheap single-token LLM call:

| Intent | Description |
|--------|-------------|
| `recommend_hardware` | Suggests devices and entities for an automation request |
| `create_automation` | Generates and inserts a new automation via the HA REST API |
| `edit_automation` | Identifies which automation to change and applies the update |
| `delete_automation` | Finds and deletes an automation by name (fuzzy matching) |
| `list_automations` | Returns a formatted list of all automations |

Device and automation data is cached (30s TTL). The agent includes a self-correction loop for hardware selection — if the LLM returns `can_fulfill=true` with an empty hardware list, it prompts for a correction automatically.

### CodeAgent & MLAgent

Pre-built agents for code execution and ML inference. `CodeAgent` runs arbitrary Python in a sandboxed subprocess. `MLAgent` wraps YOLO and anomaly detection models (`AnomalyDetectorAgent`) for computer vision tasks over MQTT — useful for smart building sensor streams.

---

## 12. Remote Nodes & Edge Deployment

AgentFlow can run agents on any machine on your network — Raspberry Pi, VM, cloud server, or any device with Python 3.10+. The edge node only needs a single file and one pip package.

### How It Works

```
[Main machine]                        [Raspberry Pi / Edge node]
main_actor ──MQTT──► nodes/{name}/spawn ──► remote_runner.py
                                               │  compiles + runs agent
                                               │  heartbeats every 10s
dashboard  ◄──MQTT── agents/{id}/heartbeat ◄───┘
```

The `remote_runner.py` is fully self-contained — it reimplements the DynamicAgent contract inline without importing anything from the agentflow package. Remote agents appear in the dashboard and respond to MQTT commands exactly like local agents.

### Edge Node Requirements

```bash
# That's it — one package, one file
pip install aiomqtt --break-system-packages
python3 remote_runner.py --broker 192.168.1.10 --name rpi-kitchen
```

The broker address must be reachable **from the Pi** (your main machine's LAN IP, not `localhost`).

### Deploying a Node

The installer agent handles SSH deployment — no manual file copying needed.

**From the CLI:**

```
/deploy rpi-kitchen
```

This will:
1. Discover the Pi on your LAN (mDNS first, then port-22 scan)
2. Prompt for SSH user, password, and your MQTT broker IP
3. Upload `remote_runner.py` via SFTP
4. Install `aiomqtt` on the Pi
5. Start the runner in the background
6. The node appears in `/nodes` within ~15 seconds

**From the chat:**

```
set up my Raspberry Pi at 192.168.1.50 as a node called rpi-kitchen
```

The LLM will call `delegate_to_installer` with a `node_deploy` action automatically.

### Spawning Agents on a Remote Node

Add `"node"` to any spawn block:

```json
<spawn>
{
  "name": "temp-sensor",
  "node": "rpi-kitchen",
  "type": "dynamic",
  "description": "Reads temperature from DHT22 and publishes to MQTT",
  "poll_interval": 30,
  "code": "
async def setup(agent):
    await agent.log('Sensor ready on ' + agent.node)

async def process(agent):
    import random
    temp = round(20 + random.uniform(-2, 2), 1)
    await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C', 'node': agent.node})
  "
}
</spawn>
```

Or just ask in chat: *"spawn a temperature sensor agent on rpi-kitchen"*

### Installing Packages on a Node

Before spawning an agent that needs hardware libraries:

```
/deploy-pkg 192.168.1.50 adafruit-circuitpython-dht RPi.GPIO
```

Or include `"install"` in the spawn block — the remote runner will pip-install them before starting the agent.

### Migrating Agents Between Nodes

Move a running agent to a different machine without stopping it manually:

```
/migrate temp-sensor rpi-bedroom
```

Or via chat: *"move temp-sensor to rpi-bedroom"*

The system stops the agent on its current node, starts it fresh on the target, and updates the spawn registry so it restores to the right machine on the next restart.

### Viewing Connected Nodes

```
/nodes
```

Output:
```
  local                online   @main @monitor @installer @home-assistant-agent
  rpi-kitchen          online   @temp-sensor
  rpi-bedroom          OFFLINE  (no agents)
```

A node is considered online if it sent a heartbeat in the last 30 seconds.

### Remote Agent API

Remote agents have the same `agent.*` API as local agents, with one addition and one limitation:

| Feature | Local | Remote |
|---------|-------|--------|
| `agent.publish(topic, data)` | ✅ | ✅ |
| `agent.log(msg)` / `agent.alert(msg)` | ✅ | ✅ |
| `agent.persist(key, val)` / `agent.recall(key)` | ✅ | ✅ (JSON file on the Pi) |
| `agent.send_to(name, payload)` | ✅ | ✅ (via MQTT round-trip) |
| `agent.node` | ❌ | ✅ (node name string) |
| `agent.llm.chat(prompt)` | ✅ | ❌ (no LLM provider on edge) |

For LLM reasoning from a remote agent, use `agent.send_to('main', {'text': prompt})` — main will call its LLM and return the result over MQTT.

### Installer Agent — Remote Actions

The installer agent gained three new actions for node management:

| Action | Description |
|--------|-------------|
| `node_deploy` | Full bootstrap: upload runner + install aiomqtt + start process |
| `node_install` | Install pip packages on a running node via SSH |
| `node_run` | Run any shell command on a remote node via SSH |

All three accept `host`, `user`, and either `password` or `key_path` for SSH auth.

---

## 13. Installation & Configuration

### Quick Start

```bash
git clone https://github.com/your-org/agentflow
cd agentflow
python -m venv myenv

# Windows
myenv\Scripts\activate
# Mac/Linux
source myenv/bin/activate

pip install -r requirements.txt

# Set your LLM key
export ANTHROPIC_API_KEY=sk-ant-...

# Optional: Home Assistant
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN=your_long_lived_token

# Start
python -m agentflow
```

### MQTT Broker

AgentFlow requires an MQTT broker. The simplest option is Mosquitto running locally:

```bash
# Windows (after installing Mosquitto)
mosquitto -v

# Docker
docker run -it -p 1883:1883 eclipse-mosquitto
```

By default AgentFlow connects to `localhost:1883`. Override with `--mqtt-host` and `--mqtt-port`.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (primary LLM) |
| `OPENAI_API_KEY` | OpenAI key (alternative LLM) |
| `NIM_API_KEY` | NVIDIA NIM key (free tier — get at build.nvidia.com) |
| `HA_URL` | Home Assistant base URL (e.g. `http://homeassistant.local:8123`) |
| `HA_TOKEN` | HA long-lived access token |
| `DISCORD_TOKEN` | Discord bot token (for `--interface discord`) |
| `TWILIO_ACCOUNT_SID` | Twilio account SID (for `--interface whatsapp`) |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | Twilio WhatsApp sender number |

---

## 14. Troubleshooting

### Conversation history corruption (400 Bad Request loop)

If you see repeated `400` errors from the Anthropic API with `"Input should be a valid dictionary"`, the persisted conversation history has been corrupted. Run the included cleanup script once:

```bash
python fix_history.py
```

Then restart AgentFlow. The LLM agent also sanitizes history on every load and before every API call as a belt-and-suspenders guard.

### Spawned agent takes too long to appear

AgentFlow checks whether required packages are already importable before calling the installer. If a package is already installed, the agent spawns instantly. If the installer is called, it now echoes the `task_id` back in its reply so the waiting future resolves immediately rather than sitting at the 120-second timeout.

### False "unresponsive" alerts for healthy agents

The monitor uses two liveness signals: `STATUS_RESPONSE` messages (from the 15-second ping cycle) and `metrics.last_heartbeat` (updated every 10 seconds by every actor automatically). Infrastructure agents (monitor, installer, main, code-agent, anomaly-detector, home-assistant-agent) are excluded from user-facing notifications even if they are temporarily quiet.

---

## Appendix: File Structure

```
agentflow/
├── main.py                        Entry point — CLI args, actor system setup, supervision tree
├── remote_runner.py               Self-contained edge node runner — deploy to any Pi or machine
├── monitor_server.py              Serves built frontend + proxies /mqtt to Mosquitto WS + /ws bridge
├── monitor.html                   Standalone legacy monitor (connects to /ws)
├── fix_history.py                 One-time corrupted history cleanup utility
├── requirements.txt
│
├── core/
│   ├── actor.py                   Base Actor — mailbox, lifecycle, heartbeat, spawn, supervisor
│   └── registry.py                ActorSystem, ActorRegistry, Supervisor — routing & OTP restarts
│
├── agents/
│   ├── llm_agent.py               LLMAgent — 4 providers, streaming, cost tracking
│   ├── main_actor.py              MainActor — orchestrator, spawn parser, node tracking, migration
│   ├── dynamic_agent.py           DynamicAgent — runtime code executor, error events
│   ├── planner_agent.py           PlannerAgent — plan cache, decompose, fan-out, synthesize
│   ├── monitor_agent.py           MonitorAgent — heartbeat, error registry, recovery
│   ├── installer_agent.py         InstallerAgent — pip install locally + SSH deploy to remote nodes
│   ├── manual_agent.py            ManualAgent — 3-layer PDF search and extraction
│   ├── home_assistant_agent.py    HomeAssistantAgent — HA automation CRUD
│   ├── code_agent.py              CodeAgent — sandboxed Python execution
│   └── ml_agent.py                MLAgent, YOLOAgent, AnomalyDetectorAgent
│
└── interfaces/
    └── chat_interfaces.py         CLI (with /deploy, /migrate, /nodes), REST, Discord, WhatsApp
```

---

## 16. Rust Backend & Dashboard

AgentFlow now includes a high-performance **Rust** backend and a **Babylon.js** interactive dashboard, running alongside the Python foundation.

### Unified Entry Point

Use `run.sh` to select your backend. It respects `AGENTFLOW_BACKEND`, and in `AGENTFLOW_DEV_MODE=1` it defaults to the Python backend so local frontend development targets the Python-first path by default.

```bash
# Start with the Rust Performance Core (Default)
./run.sh

# Start with the Python Foundation (Legacy/Specialist)
AGENTFLOW_BACKEND=python ./run.sh

# Local development defaults: Python backend + REST on :8080
AGENTFLOW_DEV_MODE=1 ./run.sh
```

### Interactive Dashboard

The dashboard visualizes your agent network in real-time with:

- **3D Graph View:** Glowing Bezier arcs showing message flow.
- **Galaxy View:** Orbital representation of the agent system.
- **Agent HUD:** Real-time token cost, status, and chat.

To build and run the full stack:

```bash
make build
make up
```

*AgentFlow — built conversation by conversation.*  
