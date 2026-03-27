# Wactorz — Architecture Reference

> Wactorz is an async, multi-agent orchestration system built on the **Actor Model** with **MQTT** as the communication backbone. Every agent is an independent actor with its own message inbox; no shared mutable state exists between actors.

---

## Table of Contents

1. [High-Level Overview](#1-high-level-overview)
2. [Components](#2-components)
   - [Wactorz Backend](#wactorz-backend)
   - [Mosquitto (MQTT Broker)](#mosquitto-mqtt-broker)
   - [nginx](#nginx)
   - [Fuseki (Optional)](#fuseki-optional)
   - [Babylon.js SPA](#babylonjs-spa)
3. [Actor Model](#3-actor-model)
   - [Lifecycle](#lifecycle)
   - [Rust Mirror](#rust-mirror)
4. [Intent Routing](#4-intent-routing)
5. [Message Flow](#5-message-flow)
   - [User → Agent](#user--agent)
   - [Agent Lifecycle Events](#agent-lifecycle-events)
6. [Persistence & State](#6-persistence--state)
7. [Memory & User Facts](#7-memory--user-facts)
8. [Reactive Pipelines](#8-reactive-pipelines)
9. [LLM Cost Tracking](#9-llm-cost-tracking)
10. [Remote Nodes & Edge Deployment](#10-remote-nodes--edge-deployment)
11. [MQTT Topic Reference](#11-mqtt-topic-reference)
12. [Deployment Modes](#12-deployment-modes)
13. [Identifiers (HLC-WIDs)](#13-identifiers-hlc-wids)
14. [File Structure](#14-file-structure)

---

## 1. High-Level Overview

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser                                                           │
│  Babylon.js SPA  ←──WebSocket──►  nginx  ←─── /ws  ──►           │
│                  ←──REST──────►          ←─── /api/ ──►           │
│                  ←──MQTT/WS───►          ←─── /mqtt ──►           │
└────────────────────────────────────────────────────────────────────┘
                                    │
                             nginx  │  (single public entry point)
                              :80   │
                                    │
          ┌─────────────────────────┼──────────────────────────────┐
          │                         │                              │
          ▼                         ▼                              ▼
  wactorz (Python)        Mosquitto (MQTT)              Fuseki (RDF)
  :8080 REST                :1883 TCP / :9001 WS          :3030 SPARQL
  :8081 WS bridge
  ┆ Rust in-sync ┆
          │
          │  pub/sub via MQTT
          ├── MainActor         (LLM orchestrator)
          ├── MonitorAgent      (health watchdog)
          ├── IOAgent           (UI gateway)
          ├── QAAgent           (safety observer)
          ├── NautilusAgent     (SSH / rsync bridge)
          ├── UDXAgent          (built-in knowledge base)
          ├── PlannerAgent      (task planning + pipeline builder)
          ├── CatalogAgent      (pre-built recipe library)
          ├── InstallerAgent    (pip + SSH deployment)
          ├── ManualAgent       (PDF search & extraction)
          ├── HomeAssistantAgent (HA automation CRUD)
          └── DynamicAgent*    (LLM-generated scripts, spawned at runtime)
```

---

## 2. Components

### Wactorz Backend

The backend is **Python-first**: the Python implementation is the primary runtime. A Rust implementation mirrors the same actor model and API contract and may run in sync alongside Python, but it is not required.

**Supported LLM providers:**

| Provider       | Key                               | Notes                                          |
|----------------|-----------------------------------|------------------------------------------------|
| Anthropic Claude | `ANTHROPIC_API_KEY`             | Default                                        |
| OpenAI         | `OPENAI_API_KEY`                  | `--llm openai`                                 |
| Ollama         | _(none)_                          | Local models, `--llm ollama --ollama-model llama3` |
| NVIDIA NIM     | `NIM_API_KEY`                     | Free tier 1000 req/month                       |
| Google Gemini  | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Free tier available                         |

**Rust crate layout (optional in-sync mirror):**

| Crate                | Role                                                              |
|----------------------|-------------------------------------------------------------------|
| `wactorz-core`       | `Actor` trait, `ActorRegistry`, message types, `EventPublisher`  |
| `wactorz-agents`     | All concrete agent implementations                                |
| `wactorz-mqtt`       | MQTT client wrapper + topic constants                             |
| `wactorz-interfaces` | REST API (axum), WebSocket bridge, interactive CLI                |
| `wactorz-server`     | Binary entry point — wires everything together                   |

---

### Mosquitto (MQTT Broker)

The MQTT broker. All inter-actor and actor-to-frontend communication passes through it. In the full Docker stack, Mosquitto is not exposed directly — all traffic is proxied through nginx.

```bash
# Windows
mosquitto -v

# Docker
docker run -it -p 1883:1883 eclipse-mosquitto
```

By default Wactorz connects to `localhost:1883`. Override with `--mqtt-host` and `--mqtt-port`.

---

### nginx

The single public HTTP entry point.

| Path       | Proxied to                      |
|------------|---------------------------------|
| `/`        | `static/app/` (SPA)             |
| `/api/`    | `wactorz:8080` (REST)           |
| `/ws`      | `wactorz:8081` (WebSocket bridge)|
| `/mqtt`    | `mosquitto:9001` (MQTT over WS) |
| `/fuseki/` | `fuseki:3030` (SPARQL, path-stripped) |

---

### Fuseki (Optional)

Apache Jena Fuseki for RDF/SPARQL storage. Not required for basic operation. Used for advanced semantic querying of agent data and entity graphs.

---

### Babylon.js SPA

A Vite + TypeScript single-page application. Connects to the backend via:

- **MQTT over WebSocket** (`/mqtt`) — receives every MQTT message in real time for the live dashboard
- **REST** (`/api/`) — agent lifecycle control (pause, resume, stop, delete)
- **WebSocket** (`/ws`) — alternative MQTT re-broadcast endpoint

---

## 3. Actor Model

Each agent is an **Actor**: an independent unit with its own async message loop, mailbox (`asyncio.Queue`), and lifecycle. Actors never share memory. They communicate by sending typed `Message` objects via the `ActorRegistry`, which maps actor IDs to actor instances.

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

### Lifecycle

```
CREATED → RUNNING → PAUSED → STOPPED
                  ↘ FAILED
```

`ActorSystem::spawn_actor(box)` registers the actor in the `ActorRegistry` and starts its run loop. The run loop is a `select!` over the mailbox channel and a heartbeat interval.

### Rust Mirror

```rust
#[async_trait]
pub trait Actor: Send {
    fn id(&self)       -> String;
    fn name(&self)     -> &str;
    fn state(&self)    -> ActorState;
    fn mailbox(&self)  -> mpsc::Sender<Message>;

    async fn on_start(&mut self)                     -> Result<()>;
    async fn handle_message(&mut self, msg: Message) -> Result<()>;
    async fn on_heartbeat(&mut self)                 -> Result<()>;
    async fn on_stop(&mut self)                      -> Result<()> { Ok(()) }
    async fn run(&mut self)                          -> Result<()>;
}
```

---

## 4. Intent Routing

Every user message goes through a single cheap LLM call that classifies it before any further processing:

| Intent     | Description                                                    | Route                  |
|------------|----------------------------------------------------------------|------------------------|
| `HA`       | Direct Home Assistant action — lights, devices, automations   | `home-assistant-agent` |
| `PIPELINE` | Reactive rule — "if X then Y", event-driven logic             | `PlannerAgent`         |
| `OTHER`    | General conversation, coding, questions                        | `main` LLM             |

Explicit prefixes (`coordinate:`, `plan:`, `pipeline:`, `@agent-name`) always win before classification.

```
coordinate: get the weather and search AI news, then combine them  → PlannerAgent (task mode)
if the door opens send me a Discord message                        → PlannerAgent (pipeline mode)
@home-assistant-agent turn on the lights                           → HA direct
what is the capital of France?                                     → main LLM
```

---

## 5. Message Flow

### User → Agent

```
Browser IO bar
  │  publishes  io/chat  { from: "user", content: "@agent-name text" }
  ▼
Mosquitto
  │  subscribed by wactorz backend
  ▼
MQTT event loop  →  IOAgent mailbox
  │
  ▼
IOAgent.handle_message()
  │  parses @mention, looks up registry
  ▼
target actor mailbox  →  handle_message()  →  LLM call (if needed)
  │
  ▼
actor publishes  agents/{id}/chat  { from: actor, to: "user", content: … }
  │
  ▼
Mosquitto  →  WebSocket bridge  →  Browser
```

### Agent-to-Agent

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
```

### Agent Lifecycle Events

Every agent publishes to its own MQTT topics on lifecycle events:

| Event        | Topic                   | Triggered by         |
|--------------|-------------------------|----------------------|
| Spawn        | `agents/{id}/spawn`     | `on_start()`         |
| Heartbeat    | `agents/{id}/heartbeat` | periodic tick (10s)  |
| State change | `agents/{id}/status`    | state mutation       |
| Alert        | `agents/{id}/alert`     | error condition      |
| Chat reply   | `agents/{id}/chat`      | `handle_message()`   |
| Error        | `agents/{id}/errors`    | exception in any phase |
| Metrics      | `agents/{id}/metrics`   | after each LLM call  |

---

## 6. Persistence & State

Every actor has access to a simple key-value persistence API backed by pickle files in the `state/` directory. State is written to disk **immediately on every `persist()` call** — not just on graceful shutdown — so no state is ever lost on Ctrl+C or crashes.

```python
agent.persist('my_key', {'count': 42, 'data': [...]})   # immediate disk write
value = agent.recall('my_key', default={})               # read
```

**What is persisted:**

| What                  | Where                         |
|-----------------------|-------------------------------|
| Conversation history  | `state/main/state.pkl`        |
| Rolling summary       | `state/main/state.pkl`        |
| User facts            | `state/main/state.pkl`        |
| Pipeline rules        | `state/main/state.pkl`        |
| Webhook URLs          | `state/main/state.pkl`        |
| Plan cache (24h TTL)  | `state/planner/state.pkl`     |
| PDF content           | `state/{agent-name}/state.pkl`|
| Spawn registry        | `state/main/state.pkl`        |

> **Note:** The `state/` directory is auto-created and should never be committed to git.

### Rolling Conversation History

`LLMAgent` keeps conversation history bounded via automatic rolling summarization:

- History is kept in RAM up to `summarize_threshold` messages (default: 30)
- When exceeded, the oldest half is compressed into a dense factual summary (~400 tokens)
- The summary is prepended as context to every subsequent LLM call — no facts are ever dropped
- A chain of summaries accumulates over time as the conversation grows
- Both `conversation_history` and `history_summary` are persisted after every exchange

If you encounter a corrupted history from a previous session, run `fix_history.py` once to clean it up.

---

## 7. Memory & User Facts

`MainActor` automatically extracts and remembers durable facts from every conversation — no explicit commands needed.

**What gets remembered:**

- Home Assistant URLs and entity IDs
- User name and preferences
- Webhook URLs and API keys
- Device names, locations, and areas
- Explicit configuration or setup details

Facts are stored in a persistent `_user_facts` dict and injected into main's system prompt on every startup.

**Memory commands:**

| Command                    | Description                                   |
|----------------------------|-----------------------------------------------|
| `/memory`                  | Show all stored user facts and conversation summary |
| `/memory clear`            | Wipe all facts and the conversation summary   |
| `/memory forget <key>`     | Remove one specific fact by its key           |

**Notification webhook commands:**

| Command                     | Description                 |
|-----------------------------|-----------------------------|
| `/webhook`                  | List stored webhook URLs    |
| `/webhook discord <url>`    | Save a Discord webhook URL  |
| `/webhook slack <url>`      | Save a Slack webhook URL    |
| `/webhook telegram <url>`   | Save a Telegram webhook URL |

Webhook URLs are automatically injected into pipeline prompts so generated pipeline agents always use your real URL.

---

## 8. Reactive Pipelines

Any message describing a conditional or event-driven behavior is automatically routed to the pipeline builder via the `PIPELINE` intent.

**Natural language examples:**

```
if the door opens, send me a Discord message
when the kitchen temperature goes above 28 degrees, turn on the air conditioner
if a person is detected on my webcam, turn on the living room lights
whenever the lamp in the living room turns on, notify me on Discord
```

### Wiring Patterns

The pipeline builder uses five canonical patterns:

| Pattern | Trigger                   | Action                         | Agents spawned                              |
|---------|---------------------------|--------------------------------|---------------------------------------------|
| 1       | HA sensor state change    | HA service call (light/switch) | dynamic filter agent + `ha_actuator`        |
| 2       | HA sensor state change    | Discord/webhook notification   | dynamic agent                               |
| 3       | Webcam object detection   | HA service call                | dynamic YOLO agent + `ha_actuator`          |
| 4       | Webcam object detection   | Discord/webhook notification   | dynamic YOLO agent + dynamic notify agent   |
| 5       | Timer/schedule            | HA service call                | dynamic timer agent + `ha_actuator`         |

**Pattern 1 note:** requires a dynamic filter agent because HA state is nested under `new_state.state` — the `ha_actuator`'s `detection_filter` only matches top-level payload keys, so the filter agent extracts the state and re-publishes a clean trigger.

### Pipeline Architecture

```
DynamicAgent (sensor/filter) → MQTT topic → HomeAssistantActuatorAgent → HA service call
```

All pipeline agents are registered in the spawn registry and auto-restored on restart.

---

## 9. LLM Cost Tracking

Every LLM call across every agent accumulates token usage into three counters: `total_input_tokens`, `total_output_tokens`, and `total_cost_usd`. These are visible per-agent in the dashboard and via `/cost` in the CLI.

**Google Gemini pricing (per 1M tokens, standard context ≤200K):**

| Model                  | Input  | Output  | Notes                         |
|------------------------|--------|---------|-------------------------------|
| `gemini-2.5-flash-lite`| $0.10  | $0.40   | Cheapest, fast, free tier     |
| `gemini-2.0-flash`     | $0.10  | $0.40   | Fast & capable, free tier     |
| `gemini-2.5-flash`     | $0.30  | $2.50   | Default, hybrid reasoning     |
| `gemini-2.5-pro`       | $1.25  | $10.00  | Best for coding & complex tasks |
| `gemini-3.1-pro`       | $2.00  | $12.00  | Flagship, no free tier        |

Pro models charge 2× for prompts above 200K tokens. Get a free API key at [aistudio.google.com](https://aistudio.google.com).

Cost is tracked for all five providers (Anthropic, OpenAI, Ollama free, NIM free/paid, Google Gemini). The `HomeAssistantAgent` tracks costs across all 7 of its internal LLM calls.

---

## 10. Remote Nodes & Edge Deployment

Wactorz can run agents on any machine on your network — Raspberry Pi, VM, cloud server, or any device with Python 3.10+.

```
[Main machine]                        [Raspberry Pi / Edge node]
main_actor ──MQTT──► nodes/{name}/spawn ──► remote_runner.py
                                               │  compiles + runs agent
                                               │  heartbeats every 10s
dashboard  ◄──MQTT── agents/{id}/heartbeat ◄───┘
```

The `remote_runner.py` is fully self-contained — it reimplements the DynamicAgent contract inline without importing anything from the wactorz package.

**Edge node requirements:**

```bash
pip install aiomqtt --break-system-packages
python3 remote_runner.py --broker 192.168.1.10 --name rpi-kitchen
```

**Deploying a node:**

```
/deploy rpi-kitchen
```

This will discover the Pi on your LAN, prompt for SSH credentials, upload `remote_runner.py`, install `aiomqtt`, start the runner, and make the node appear in `/nodes` within ~15 seconds.

**Viewing connected nodes:**

```
/nodes

  local                online   @main @monitor @installer @home-assistant-agent
  rpi-kitchen          online   @temp-sensor
  rpi-bedroom          OFFLINE  (no agents)
```

**Remote vs local agent API:**

| Feature                         | Local | Remote                          |
|---------------------------------|-------|---------------------------------|
| `agent.publish(topic, data)`    | ✅    | ✅                              |
| `agent.log(msg)` / `alert(msg)` | ✅    | ✅                              |
| `agent.persist()` / `recall()`  | ✅    | ✅ (JSON file on the node)       |
| `agent.send_to(name, payload)`  | ✅    | ✅ (via MQTT round-trip)         |
| `agent.node`                    | ❌    | ✅ (node name string)            |
| `agent.llm.chat(prompt)`        | ✅    | ❌ (use `send_to('main', ...)`) |

**Migrating agents between nodes:**

```
/migrate temp-sensor rpi-bedroom
```

---

## 11. MQTT Topic Reference

| Topic                                            | Description                                              |
|--------------------------------------------------|----------------------------------------------------------|
| `agents/{id}/heartbeat`                          | Liveness pulse every 10s — name, state, metrics          |
| `agents/{id}/logs`                               | Log events, spawn notifications, user interactions       |
| `agents/{id}/errors`                             | Structured error events with phase, severity, traceback  |
| `agents/{id}/alert`                              | Alert events (heartbeat timeout or error escalation)     |
| `agents/{id}/metrics`                            | Token usage, cost, tasks completed after each LLM call   |
| `agents/{id}/completed`                          | Task completion notification with result preview         |
| `agents/{id}/actuations`                         | Fired by `HomeAssistantActuatorAgent` on each HA call    |
| `agents/by-name/{name}/task`                     | Address a task to an agent by name                       |
| `system/health`                                  | Global health snapshot every 15s                         |
| `homeassistant/state_changes/{domain}/{entity_id}`| HA state changes (published by StateBridgeAgent)        |
| `homeassistant/map/entities_with_location`        | Live entity/location map (published by MapAgent)         |
| `custom/detections/{slug}`                        | Object detection events from YOLO pipeline agents       |
| `custom/triggers/{slug}`                          | Filtered state triggers re-published by filter agents   |
| `nodes/{name}/spawn`                              | Spawn a new agent on a remote node                      |
| `nodes/{name}/stop`                               | Stop a named agent on a remote node                     |
| `nodes/{name}/migrate`                            | Move an agent from this node to another                 |
| `nodes/{name}/list`                               | Request list of agents running on a node                |
| `nodes/{name}/heartbeat`                          | Node liveness pulse — agent list, broker, timestamp     |
| `nodes/{name}/migrate_result`                     | Migration success/failure notification                  |
| `io/chat`                                         | Frontend IO bar input (fixed topic)                     |

---

## 12. Deployment Modes

| Mode                                  | Docker containers                             | Binary            |
|---------------------------------------|-----------------------------------------------|-------------------|
| **Full Docker** (`compose.yaml`)       | wactorz + nginx + mosquitto + fuseki + HA     | Inside container  |
| **Native binary** (`compose.native.yaml`) | nginx + mosquitto only                    | Runs on host OS   |

**Interfaces:**

| Interface   | Start command                                           | Notes                               |
|-------------|---------------------------------------------------------|-------------------------------------|
| CLI         | `python -m wactorz`                                     | Streaming, default                  |
| REST        | `python -m wactorz --interface rest`                    | POST `/chat`, non-streaming         |
| Discord     | `python -m wactorz --interface discord`                 | Responds when bot is mentioned      |
| Telegram    | `python -m wactorz --interface telegram`                | DM-based, self-hosted               |
| WhatsApp    | `python -m wactorz --interface whatsapp`                | Via Twilio webhook                  |
| Dashboard   | `python monitor_server.py` + open `monitor.html`        | Real-time web UI over WebSocket     |

---

## 13. Identifiers (HLC-WIDs)

All actor IDs use **HLC-WIDs** (Hybrid Logical Clock Wide IDs) from the [`waldiez-wid`](https://github.com/waldiez/wid) crate. They are:

- **Time-ordered** — sort chronologically without a database
- **Globally unique** — no coordination required
- **Human-readable** — contain a timestamp component

Message IDs use simpler **WIDs**.

---

## 14. File Structure

```
wactorz/
├── main.py                                    Entry point — CLI args, actor system setup, supervision tree
├── remote_runner.py                           Self-contained edge node runner
├── monitor_server.py                          MQTT → WebSocket bridge for dashboard
├── monitor.html                               Live web dashboard
├── fix_history.py                             One-time corrupted history cleanup utility
├── requirements.txt
│
├── core/
│   ├── actor.py                               Base Actor — mailbox, lifecycle, heartbeat, spawn, supervisor
│   └── registry.py                            ActorSystem, ActorRegistry, Supervisor — routing & OTP restarts
│
├── agents/
│   ├── llm_agent.py                           LLMAgent — 5 providers, rolling summarization, cost tracking
│   ├── main_actor.py                          MainActor — intent routing, memory, user facts, pipeline rules
│   ├── dynamic_agent.py                       DynamicAgent — runtime code executor, error events
│   ├── planner_agent.py                       PlannerAgent — task planning + reactive pipeline builder
│   ├── monitor_agent.py                       MonitorAgent — heartbeat, error registry, recovery
│   ├── installer_agent.py                     InstallerAgent — pip install locally + SSH deploy to remote nodes
│   ├── catalog_agent.py                       CatalogAgent — pre-built recipe library
│   ├── manual_agent.py                        ManualAgent — 3-layer PDF search and extraction
│   ├── home_assistant_agent.py                HomeAssistantAgent — HA automation CRUD (LLM-backed)
│   ├── home_assistant_map_agent.py            HomeAssistantMapAgent — live entity/location map
│   ├── home_assistant_state_bridge_agent.py   HomeAssistantStateBridgeAgent — state_changed → MQTT
│   ├── home_assistant_actuator_agent.py       HomeAssistantActuatorAgent — reactive MQTT→HA actuator
│   ├── code_agent.py                          CodeAgent — sandboxed Python execution
│   └── ml_agent.py                            MLAgent, YOLOAgent, AnomalyDetectorAgent
│
├── interfaces/
│   └── chat_interfaces.py                     CLI, REST, Discord, Telegram, WhatsApp
│
└── rust/                                      Optional Rust in-sync mirror
    └── crates/
        ├── wactorz-core/
        ├── wactorz-agents/
        ├── wactorz-mqtt/
        ├── wactorz-interfaces/
        └── wactorz-server/

catalogue_agents/                              Pre-built agent recipe files (loaded by CatalogAgent)
├── __init__.py
├── image_gen_agent.py                         NIM FLUX.1-dev image generation
└── doc_to_pptx_agent.py                      PDF/TXT → PowerPoint conversion

state/                                         Persisted agent state (auto-created, never commit to git)
├── main/state.pkl
├── planner/state.pkl
└── {agent-name}/state.pkl
```

---

## Environment Variables Reference

| Variable                     | Description                                                  |
|------------------------------|--------------------------------------------------------------|
| `ANTHROPIC_API_KEY`          | Claude API key (primary LLM)                                 |
| `OPENAI_API_KEY`             | OpenAI key (alternative LLM)                                 |
| `NIM_API_KEY`                | NVIDIA NIM key (free tier)                                   |
| `GEMINI_API_KEY`             | Google Gemini API key (free tier)                            |
| `HA_URL`                     | Home Assistant base URL                                      |
| `HA_TOKEN`                   | HA long-lived access token                                   |
| `HA_MAP_AGENT_OUTPUT_TOPIC`  | MQTT topic for MapAgent (default: `homeassistant/map/entities_with_location`) |
| `HA_MAP_AGENT_TARGET_ACTOR`  | Route map updates to a named actor instead of MQTT           |
| `HA_STATE_BRIDGE_OUTPUT_TOPIC` | Base MQTT topic for StateBridgeAgent                       |
| `HA_STATE_BRIDGE_DOMAINS`    | Comma-separated domain allow-list (empty = all)              |
| `HA_STATE_BRIDGE_PER_ENTITY` | `1` = per-entity sub-topics; `0` = single topic              |
| `DISCORD_BOT_TOKEN`          | Discord bot token                                            |
| `TELEGRAM_BOT_TOKEN`         | Telegram bot token from BotFather                            |
| `TELEGRAM_ALLOWED_USER_ID`   | Restrict Telegram bot to a single numeric user ID            |
| `TWILIO_ACCOUNT_SID`         | Twilio account SID (WhatsApp interface)                      |
| `TWILIO_AUTH_TOKEN`          | Twilio auth token                                            |
| `TWILIO_WHATSAPP_FROM`       | Twilio WhatsApp sender number                                |

---

*Wactorz — the 24/7 agents built for the physical world.*
