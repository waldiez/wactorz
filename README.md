# Wactorz

**Actor-Model Multi-Agent Framework**  
_Technical Reference & Developer Guide_

---

## Table of Contents

1. [What is Wactorz?](#1-what-is-wactorz)
2. [Architecture](#2-architecture)
3. [Agent Types](#3-agent-types)
4. [Spawning Agents at Runtime](#4-spawning-agents-at-runtime)
5. [Agent-to-Agent Communication](#5-agent-to-agent-communication)
6. [Health Monitoring & Error Recovery](#6-health-monitoring--error-recovery)
7. [Persistence & State](#7-persistence--state)
8. [Memory & User Facts](#8-memory--user-facts)
9. [Reactive Pipelines](#9-reactive-pipelines)
10. [LLM Cost Tracking](#10-llm-cost-tracking)
11. [Interfaces](#11-interfaces)
12. [MQTT Topic Reference](#12-mqtt-topic-reference)
13. [Built-in Specialist Agents](#13-built-in-specialist-agents)
14. [Catalog Agent — Pre-built Recipe Library](#14-catalog-agent--pre-built-recipe-library)
15. [Remote Nodes & Edge Deployment](#15-remote-nodes--edge-deployment)
16. [Installation & Configuration](#16-installation--configuration)
17. [Troubleshooting](#17-troubleshooting)
18. [File Structure](#appendix-file-structure)

---

## 1. What is Wactorz?

Wactorz is an asynchronous, actor-model multi-agent framework built from scratch in Python. It allows an LLM orchestrator ("main") to spawn, coordinate, monitor, and retire live software agents at runtime — without any code restart or predefined agent types.

The core idea is simple: you talk to the system in natural language. The LLM writes Python code, wraps it in a `<spawn>` block, and a new agent appears — running in its own async actor, connected to all other agents via MQTT and direct actor messaging, and persisting its state to disk automatically.

Wactorz was born out of the need for a framework that could operate on real-world IoT data streams at the edge — something existing agent frameworks (LangGraph, CrewAI, AutoGen) were not designed for. It is lightweight enough to run on modest hardware, offline-capable, and fully async.

### Design Principles

- **Everything is an Actor** — agents communicate via messages, not function calls
- **Agents are spawned at runtime** — no hardcoded types, no restart required
- **MQTT is the nervous system** — all events, heartbeats, and results flow through topics
- **Persistence is automatic** — every agent survives a crash and restores its state
- **The LLM is the orchestrator** — it decides what agents to create and how to wire them
- **Errors are first-class** — structured error events trigger real recovery actions
- **Memory is persistent** — conversation history is summarized, user facts are extracted and remembered across restarts

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

### Intent Routing

Every user message goes through a single cheap LLM call that classifies it into one of three categories before any further processing:

| Intent | Description | Route |
|--------|-------------|-------|
| `HA` | Direct Home Assistant action — turn on lights, list devices, create automation | → `home-assistant-agent` |
| `PIPELINE` | Reactive rule — "if X then Y", "when X send me a message", any event-driven logic | → `PlannerAgent` |
| `OTHER` | General conversation, coding, questions, everything else | → `main` LLM |

This replaces all previous keyword heuristics with a single LLM classification step. Explicit prefixes (`coordinate:`, `plan:`, `pipeline:`) always win before classification.

### Core Components

| File | Layer | Role |
|------|-------|------|
| `core/actor.py` | Core | Base Actor class — mailbox, lifecycle, heartbeat, spawn, send, persist/recall |
| `core/registry.py` | Core | ActorSystem & ActorRegistry — actor registration, message routing, broadcast |
| `agents/main_actor.py` | Agent | The LLM orchestrator — intent classification, spawns agents, routes requests, memory & user facts |
| `agents/monitor_agent.py` | Agent | Health watcher — detects crashes, fires recovery actions, notifies user |
| `agents/llm_agent.py` | Agent | Base LLM agent with rolling history summarization, cost tracking, streaming, and 4 providers |
| `agents/dynamic_agent.py` | Agent | Runtime-generated agents — executes LLM-written Python code in a sandboxed namespace |
| `agents/planner_agent.py` | Agent | Multi-step task planner + reactive pipeline builder — decomposes tasks, fans out to workers, synthesizes results |
| `agents/installer_agent.py` | Agent | Package manager — installs pip packages locally and on remote nodes via SSH |
| `agents/catalog_agent.py` | Agent | Recipe library — holds pre-built agent configs and spawns them on request without requiring code |
| `agents/manual_agent.py` | Agent | PDF specialist — 3-layer search strategy to find and extract manual content |
| `agents/home_assistant_agent.py` | Agent | Unified HA agent — hardware recommendations and automation CRUD via HA REST API |
| `agents/home_assistant_map_agent.py` | Agent | Live entity/location map via HA WebSocket |
| `agents/home_assistant_state_bridge_agent.py` | Agent | HA `state_changed` → MQTT bridge |
| `agents/home_assistant_actuator_agent.py` | Agent | Reactive MQTT→HA actuator — subscribes to topics, calls HA services |
| `interfaces/chat_interfaces.py` | I/O | CLI (streaming), REST, Discord, WhatsApp — all call `process_user_input[_stream]` |
| `monitor_server.py` | I/O | MQTT→WebSocket bridge that feeds the live dashboard |
| `monitor.html` | I/O | Real-time web dashboard — agent cards, logs, cost meters, error alerts |

---

## 3. Agent Types

### LLMAgent (base)

All LLM-backed agents inherit from `LLMAgent`, which inherits from `Actor`. It manages conversation history with automatic rolling summarization (persisted to disk), tracks token usage and cost across 4 providers, and supports both blocking and streaming responses.

**Supported LLM providers:**

| Provider | Key | Notes |
|----------|-----|-------|
| Anthropic Claude | `ANTHROPIC_API_KEY` | Default |
| OpenAI | `OPENAI_API_KEY` | `--llm openai` |
| Ollama | _(none)_ | Local models, `--llm ollama --ollama-model llama3` |
| NVIDIA NIM | `NIM_API_KEY` | Free tier 1000 req/month, `--llm nim --nim-model meta/llama-3.3-70b-instruct` |
| Google Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Free tier available, `--llm gemini --gemini-model gemini-2.5-flash` |

### DynamicAgent

The heart of Wactorz. When the LLM writes a spawn block, a `DynamicAgent` is created with that code compiled into its namespace. Three optional async functions can be defined:

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
| `await agent.log(msg)` | Publish a log event |
| `await agent.publish(topic, data)` | Publish to an MQTT topic |
| `agent.persist(key, value)` / `agent.recall(key)` | Durable key-value state |
| `agent.state["key"]` | In-memory dict (cleared on restart) |
| `agent.llm.chat(prompt)` | Call the LLM |
| `agent.send_to(name, payload)` | Send a task to another agent by name |
| `agent.delegate(name, payload)` | Same, with cleaner syntax |
| `agent.send_to_many(tasks)` | Fan-out to multiple agents in parallel |
| `agent.agents()` | List all currently running agents |

### MainActor

The user-facing orchestrator. Every message you type is processed by main, which:

1. Intercepts slash-commands (`/rules`, `/memory`, `/webhook`, `/topics`, etc.) without any LLM call
2. Classifies intent with a single LLM call: `HA`, `PIPELINE`, or `OTHER`
3. Routes `HA` requests to `home-assistant-agent`
4. Routes `PIPELINE` requests to `PlannerAgent`
5. Handles `OTHER` with its own streaming LLM conversation
6. Extracts and persists user facts in the background after every response
7. Drains any pending monitor notifications and prepends them to the response
8. Parses `<spawn>` blocks in the LLM output and creates agents automatically

### PlannerAgent

Spawned on-demand for two distinct modes:

**Task planning mode** (complex multi-step tasks):
1. Check plan cache — reuse plan structure if the task is similar to a recent one (24h TTL)
2. Discover all running worker agents
3. Ask the LLM to decompose the task into a dependency graph of steps
4. Spawn any missing agents declared in the plan (with `spawn_config`)
5. Execute parallel steps with `asyncio.gather`, inject context into dependent steps
6. Synthesize all results into a clean user-facing answer
7. Cache the plan to disk, self-terminate after 2 seconds

**Pipeline mode** (reactive if/when/whenever rules):
1. Query `home-assistant-agent` for real entity IDs from your HA instance
2. Feasibility check — verifies required entity types exist, surfaces a clear error if not
3. LLM designs the agent wiring using canonical patterns (see Section 9)
4. Spawn `ha_actuator` agents (for HA service calls) and `dynamic` agents (for filtering, webcam, notifications)
5. Register each rule in main's pipeline registry for persistence and listing

**Trigger the planner explicitly or automatically:**

```
coordinate: get the weather in Athens and search for AI news, then combine them
plan: load the Philips manual and answer the cleaning question
@planner   any complex multi-step task
if the door opens send me a Discord message    ← auto-detected as PIPELINE
```

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

Agents with packages in `"install"` are spawned in the background. A fast-path checks whether packages are already importable first — if they are, spawning is instant. All spawned agents are saved to the spawn registry and automatically restored on the next startup.

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

Wactorz has a four-layer error handling system. Errors are first-class events, not just log lines.

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

Every actor has access to a simple key-value persistence API backed by pickle files in the `state/` directory. State is written to disk **immediately on every `persist()` call** — not just on graceful shutdown — so no state is ever lost on Ctrl+C or crashes.

```python
# Inside any agent
agent.persist('my_key', {'count': 42, 'data': [...]})   # write (immediate disk write)
value = agent.recall('my_key', default={})               # read
```

Used internally for:

- Conversation history (`LLMAgent`) — sanitized on every load, with rolling summarization
- Rolling summary (`LLMAgent`) — compressed history surviving beyond the context window
- User facts (`MainActor`) — durable facts extracted from every conversation exchange
- Pipeline rules (`MainActor`) — spawn registry for reactive rules, with agent lists
- Notification webhook URLs (`MainActor`) — auto-injected into pipeline prompts
- Plan cache (`PlannerAgent`) — 24h TTL, invalidated if required agents are gone
- Loaded PDF content (`ManualAgent`) — avoids re-downloading on repeated questions
- Spawn registry (`MainActor`) — restores all agents on startup

### Rolling Conversation History

`LLMAgent` keeps conversation history bounded and lossless via automatic rolling summarization:

- History is kept in RAM up to `summarize_threshold` messages (default: 30)
- When that threshold is exceeded, the **oldest half** is compressed into a dense factual summary using the LLM (~400 tokens)
- The summary is prepended as context to every subsequent LLM call — no facts are ever dropped
- A chain of summaries accumulates over time as the conversation grows
- Both `conversation_history` and `history_summary` are persisted after every exchange

Conversation history is sanitized on every load — any corrupted entries are stripped before the API is called. If you encounter a corrupted history from a previous session, run `fix_history.py` once to clean it up.

---

## 8. Memory & User Facts

Main automatically extracts and remembers durable facts from every conversation — no explicit commands needed.

### How It Works

After every response, main runs a background LLM task that scans the exchange for durable facts worth remembering long-term:

- Home Assistant URLs and entity IDs
- User name and preferences
- Webhook URLs and API keys
- Device names, locations, and areas
- Any explicit configuration or setup details mentioned by the user

These are stored in a persistent `_user_facts` dict and injected into main's system prompt on every startup, so main always knows who you are and what your setup looks like — even after a restart.

### Memory Commands

| Command | Description |
|---------|-------------|
| `/memory` | Show all stored user facts and the current conversation summary |
| `/memory clear` | Wipe all facts and the conversation summary |
| `/memory forget <key>` | Remove one specific fact by its key |

### Notification Webhooks

Webhook URLs for Discord, Slack, and Telegram are stored separately and automatically injected into pipeline prompts — so generated pipeline agents always use your real URL without you having to provide it again.

| Command | Description |
|---------|-------------|
| `/webhook` | List stored webhook URLs |
| `/webhook discord <url>` | Save a Discord webhook URL |
| `/webhook slack <url>` | Save a Slack webhook URL |
| `/webhook telegram <url>` | Save a Telegram webhook URL |

You can also paste a webhook URL directly into any message — it is detected automatically and saved.

---

## 9. Reactive Pipelines

Wactorz can set up persistent reactive rules that run continuously in the background. Any message describing a conditional or event-driven behavior is automatically routed to the pipeline builder via the `PIPELINE` intent.

### Natural Language Examples

```
if the door opens, send me a Discord message
when the temperature in the kitchen goes above 28 degrees, turn on the air conditioner
if a person is detected on my webcam, turn on the living room lights
whenever the lamp in the living room turns on, notify me on Discord
```

No prefix needed — the intent classifier recognises these automatically.

### How It Works

The `PlannerAgent` handles pipeline requests:

1. **Entity discovery** — queries `home-assistant-agent` for real entity IDs from your HA instance
2. **Feasibility check** — verifies the required entity types exist; surfaces a clear error if not
3. **Agent design** — LLM selects the correct wiring pattern and generates spawn configs with real entity IDs
4. **Spawning** — agents are created and registered in the spawn registry (auto-restore on restart)
5. **Rule registration** — the rule is saved in main's pipeline registry with its agent list

### Wiring Patterns

The pipeline builder uses five canonical patterns:

| Pattern | Trigger | Action | Agents spawned |
|---------|---------|--------|----------------|
| 1 | HA sensor state change | HA service call (light/switch/climate) | dynamic filter agent + `ha_actuator` |
| 2 | HA sensor state change | Discord/webhook notification | dynamic agent |
| 3 | Webcam object detection | HA service call | dynamic YOLO agent + `ha_actuator` |
| 4 | Webcam object detection | Discord/webhook notification | dynamic YOLO agent + dynamic notify agent |
| 5 | Timer/schedule | HA service call | dynamic timer agent + `ha_actuator` |

Pattern 1 requires a dynamic filter agent because HA state is nested under `new_state.state` — the `ha_actuator`'s `detection_filter` only matches top-level payload keys, so the filter agent extracts the state and re-publishes a clean trigger.

### Pipeline Commands

| Command | Description |
|---------|-------------|
| `/rules` | List all active pipeline rules with agent status (green/red) and creation time |
| `/rules delete <rule_id>` | Stop all agents for a rule and remove it from the registry |

### HomeAssistantActuatorAgent

The actuator end of every HA pipeline. Each instance subscribes to one or more MQTT topics, evaluates optional HA entity conditions, enforces a configurable cooldown, and calls HA services via a persistent WebSocket connection.

```
DynamicAgent (sensor/filter) → MQTT topic → HomeAssistantActuatorAgent → HA service call
```

One instance is spawned per automation, configured with an `ActuatorConfig`:

```python
ActuatorConfig(
    automation_id    = "person-light",
    mqtt_topics      = ["custom/detections/living-room"],
    detection_filter = {"detected": True},
    cooldown_seconds = 10.0,
    conditions       = [
        ActuatorCondition(entity_id="sun.sun", attribute="state", operator="eq", value="below_horizon")
    ],
    actions          = [
        ActuatorAction(domain="light", service="turn_on", entity_id="light.living_room")
    ],
)
```

Detection filter values can be plain literals (equality) or operator dicts such as `{"gte": 0.7}`. Supported operators: `eq`, `ne`, `gt`, `lt`, `gte`, `lte`. Conditions use AND logic and query live HA entity state via WebSocket.

---

## 10. LLM Cost Tracking

Every LLM call across every agent accumulates token usage into three counters: `total_input_tokens`, `total_output_tokens`, and `total_cost_usd`. These are visible per-agent in the dashboard and via `/cost` in the CLI.

Cost is tracked for all five providers (Anthropic, OpenAI, Ollama free, NIM free/paid, Google Gemini). The `HomeAssistantAgent` tracks costs across all 7 of its internal LLM calls: classification, hardware selection, correction retry, automation generation, delete confirmation, edit identification, and edit generation.

### Google Gemini Pricing (per 1M tokens, standard context ≤200K)

| Model | Input | Output | Notes |
|-------|-------|--------|-------|
| `gemini-2.5-flash-lite` | $0.10 | $0.40 | Cheapest, fast, free tier |
| `gemini-2.0-flash` | $0.10 | $0.40 | Fast & capable, free tier |
| `gemini-2.5-flash` | $0.30 | $2.50 | Default, hybrid reasoning, free tier |
| `gemini-2.5-pro` | $1.25 | $10.00 | Best for coding & complex tasks |
| `gemini-3.1-pro` | $2.00 | $12.00 | Flagship, no free tier |

Pro models charge 2x for prompts above 200K tokens. Get a free API key at [aistudio.google.com](https://aistudio.google.com).

---

## 11. Interfaces

### CLI (Streaming)

```bash
python -m wactorz                                              # Anthropic Claude (default)
python -m wactorz --llm openai
python -m wactorz --llm ollama --ollama-model llama3
python -m wactorz --llm nim --nim-model meta/llama-3.3-70b-instruct
python -m wactorz --llm gemini                                         # gemini-2.5-flash default
python -m wactorz --llm gemini --gemini-model gemini-2.5-pro
python -m wactorz --interface discord --discord-token YOUR_TOKEN
```

**CLI commands:**

| Command | Description |
|---------|-------------|
| `/agents` | List all running agents with type and status |
| `/nodes` | List remote nodes with online/offline status and their agents |
| `/rules` | List all active pipeline rules |
| `/rules delete <id>` | Stop and delete a pipeline rule by its ID |
| `/memory` | Show stored user facts and conversation summary |
| `/memory clear` | Wipe all stored memory |
| `/memory forget <key>` | Remove a specific stored fact |
| `/webhook <service> <url>` | Store a notification webhook URL |
| `/topics` | List known MQTT topics and their publishing agents |
| `/cost` | Show per-agent token usage and cost breakdown |
| `/clear` | Clear the main agent's conversation history |
| `/clear-plans` | Wipe the planner's plan cache |
| `/deploy <node-name>` | Bootstrap a new remote node via SSH |
| `/deploy-pkg <host> <pkg...>` | Install pip packages on a remote node |
| `/migrate <agent> <node>` | Move a running agent to a different node |
| `/help` | Show all available commands |
| `@agent-name` | Route your next message directly to a specific agent |

### REST API

Start with `--interface rest` (default port 8080). Send `POST` requests to `/chat` with `{"message": "..."}`. Responses are blocking (non-streaming). Suitable for integration with other services.

### Discord

Set `DISCORD_BOT_TOKEN` and start with `--interface discord`. The bot responds to messages prefixed with `!` (e.g. `!turn on the lights`). Make sure to enable the **Message Content Intent** in your Discord Developer Portal under Bot → Privileged Gateway Intents.

### WhatsApp

Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_FROM` and start with `--interface whatsapp`. Wactorz runs an aiohttp webhook server that receives incoming messages from Twilio. The same `process_user_input()` pipeline handles all interfaces.

### Live Dashboard

Start `monitor_server.py` alongside wactorz. Open `monitor.html` in a browser. The dashboard shows real-time agent cards, log streams, token cost meters, spawn/stop controls, and error alerts — all fed via MQTT over WebSocket.

---

## 12. MQTT Topic Reference

| Topic | Description |
|-------|-------------|
| `agents/{id}/heartbeat` | Liveness pulse every 10s — name, state, metrics |
| `agents/{id}/logs` | Log events, spawn notifications, user interactions |
| `agents/{id}/errors` | Structured error events with phase, severity, traceback |
| `agents/{id}/alert` | Alert events (heartbeat timeout or error escalation) |
| `agents/{id}/metrics` | Token usage, cost, tasks completed after each LLM call |
| `agents/{id}/completed` | Task completion notification with result preview |
| `agents/{id}/actuations` | Fired by `HomeAssistantActuatorAgent` on each HA service call |
| `agents/by-name/{name}/task` | Address a task to an agent by name (used by remote agents) |
| `system/health` | Global health snapshot every 15s — running/stopped/failed counts |
| `homeassistant/state_changes/{domain}/{entity_id}` | HA state changes (published by StateBridgeAgent) |
| `homeassistant/map/entities_with_location` | Live entity/location map (published by MapAgent) |
| `custom/detections/{slug}` | Object detection events from YOLO pipeline agents |
| `custom/triggers/{slug}` | Filtered state triggers re-published by pipeline filter agents |
| `nodes/{name}/spawn` | Spawn a new agent on a remote node |
| `nodes/{name}/stop` | Stop a named agent on a remote node |
| `nodes/{name}/migrate` | Move an agent from this node to another |
| `nodes/{name}/list` | Request list of agents running on a node |
| `nodes/{name}/heartbeat` | Node liveness pulse — agent list, broker, timestamp |
| `nodes/{name}/migrate_result` | Migration success/failure notification |

---

## 13. Built-in Specialist Agents

### ManualAgent — PDF Specialist

Finds and extracts product manuals from the web using a 3-layer search strategy:

1. **Direct URL construction** — for known brands (e.g. Philips), tries manufacturer CDNs directly with a HEAD request
2. **DuckDuckGo search** — with multiple key name fallbacks (`href`, `url`, `link`)
3. **Bing HTML scrape** — parses HTML for PDF links and trusted manual site URLs

PDF content is extracted in memory (`pdfplumber` → `pymupdf` fallback) and stored in the agent's persistence so repeat questions don't require re-downloading.

### HomeAssistantAgent — HA Automation

Connects to your Home Assistant instance (set `HA_URL` and `HA_TOKEN`) and handles intents, classified by a cheap single-token LLM call:

| Intent | Description |
|--------|-------------|
| `recommend_hardware` | Suggests devices and entities for an automation request |
| `create_automation` | Generates and inserts a new automation via the HA REST API |
| `edit_automation` | Identifies which automation to change and applies the update |
| `delete_automation` | Finds and deletes an automation by name (fuzzy matching) |
| `list_automations` | Returns a formatted list of all automations |
| `list_areas` | Lists all Home Assistant areas |
| `list_devices` | Lists all devices |
| `list_entities` | Lists all entities |

Device and automation data is cached (30s TTL). The agent includes a self-correction loop for hardware selection — if the LLM returns `can_fulfill=true` with an empty hardware list, it prompts for a correction automatically.

### HomeAssistantMapAgent — Live Entity Map

Maintains a live, location-enriched map of every HA device and entity. Opens a persistent WebSocket connection to Home Assistant and re-fetches the full device/entity/location dataset every time the entity registry changes, then publishes the result to MQTT (or forwards it directly to another actor by name).

**Published topic** (default): `homeassistant/map/entities_with_location`

**Task commands** (sent to agent mailbox):

| Command | Description |
|---------|-------------|
| `refresh` | Force an immediate rebuild and publish |
| `status` | Return connection state, event counter, and last error |

Configure with `HA_MAP_AGENT_OUTPUT_TOPIC` and optionally `HA_MAP_AGENT_TARGET_ACTOR` (routes the payload to another actor instead of MQTT).

### HomeAssistantStateBridgeAgent — State Change Bridge

Bridges every Home Assistant `state_changed` event to MQTT. Used as the trigger source for all HA-based reactive pipelines.

**Published topic** (default): `homeassistant/state_changes/{domain}/{entity_id}`

Key options:
- `HA_STATE_BRIDGE_DOMAINS` — comma-separated allow-list (e.g. `light,switch,sensor`); empty = all domains
- `HA_STATE_BRIDGE_PER_ENTITY` — `1` (default) splits into per-entity sub-topics; `0` sends everything to one topic

**Task commands**: `status`

### HomeAssistantActuatorAgent — Reactive Actuator

See [Section 9 — Reactive Pipelines](#9-reactive-pipelines) for full documentation.

### CodeAgent & MLAgent

Pre-built agents for code execution and ML inference. `CodeAgent` runs arbitrary Python in a sandboxed subprocess. `MLAgent` wraps YOLO and anomaly detection models (`AnomalyDetectorAgent`) for computer vision tasks over MQTT — useful for smart building sensor streams.

---

## 14. Catalog Agent — Pre-built Recipe Library

The `CatalogAgent` is a built-in agent that starts with the system and holds a library of ready-made agent recipes. Instead of writing spawn code from scratch, you ask the catalog to spawn a named agent for you — it handles everything including injecting the code, schemas, and capabilities into main's existing spawn pipeline.

### Why It Exists

Some agents are too useful to re-invent every session but too specific to hardcode into `cli.py` as permanent agents. The catalog is the middle ground: recipes live in the `catalogue_agents/` folder as plain Python files, the catalog loads them at startup, and any agent — main, planner, or the user directly — can request a spawn by name.

### Usage

**Direct (from CLI):**

```text
@catalog spawn image-gen-agent
@catalog spawn doc-to-pptx-agent
@catalog list
@catalog info doc-to-pptx-agent
```

**Natural language via main:**

```text
"spawn the image generation agent"
"what agents can you spawn for me?"
"I need to convert a PDF to PowerPoint"
```

Main discovers the catalog via `/capabilities` and routes through it automatically.

### Available Actions

| Action | Payload | Description |
|--------|---------|-------------|
| `list` | `{"action": "list"}` | Returns all available recipes with name, description, and capabilities |
| `info` | `{"action": "info", "agent": "name"}` | Returns full recipe metadata (without the code string) |
| `spawn` | `{"action": "spawn", "agent": "name"}` | Spawns the named agent via main's spawn pipeline; saves to spawn registry |

Spawned agents are registered in main's spawn registry — they survive restarts just like any manually spawned agent.

### Built-in Recipes

| Recipe | Description | Key Dependencies |
|--------|-------------|-----------------|
| `image-gen-agent` | Generates images from text prompts using NVIDIA NIM FLUX.1-dev. Returns absolute PNG path. | `requests`, NIM API key |
| `doc-to-pptx-agent` | Converts PDF or TXT documents into PowerPoint presentations. Extracts real embedded images from the PDF first; falls back to NIM FLUX generation for slides without images. | `pymupdf`, `pdfplumber`, `pptxgenjs` (Node.js) |

### Adding New Recipes

Drop a Python file into `catalogue_agents/` with an `AGENT_CODE` string (the same format as any dynamic agent), then add its entry to `catalog_agent.py`:

```python
# In catalog_agent.py — _build_catalog()
code = _load_recipe("my_new_agent.py")
if code:
    catalog["my-new-agent"] = {
        "name":         "my-new-agent",
        "type":         "dynamic",
        "description":  "What this agent does",
        "capabilities": ["keyword1", "keyword2"],
        "input_schema":  { "param": "str — description" },
        "output_schema": { "result": "str" },
        "poll_interval": 3600,
        "code":          code,
    }
```

No changes to `cli.py` or any other file needed. On next restart the recipe is available system-wide.

### image-gen-agent

Generates images from text prompts via NVIDIA NIM FLUX.1-dev and saves them as PNG files. Requires a free NIM API key (1000 credits/month at [build.nvidia.com](https://build.nvidia.com)).

**Setup:**

```text
@main remember nim_api_key = nvapi-xxxxxxxxxxxxxxxx
```

**Task payload:**

```json
{
  "prompt": "minimalist flat illustration of renewable energy",
  "output_path": "C:/Users/you/Documents/slide.png",
  "width": 1024,
  "height": 576,
  "steps": 20
}
```

**Result:** `{ "image_path": "...", "width": 1024, "height": 576, "size_kb": 312, "error": null }`

### doc-to-pptx-agent

Converts a PDF or TXT document into a polished PowerPoint presentation in four steps:

1. **Read** — extracts text via `pdfplumber` (PDF) or plain read (TXT)
2. **Extract images** — pulls real embedded images from the PDF using PyMuPDF; filters out small decorations (configurable minimum size); assigns images to slides by source-page proximity
3. **LLM outline** — calls the LLM to produce a structured JSON outline: slide titles, bullets, theme colors, and per-slide image prompts
4. **Build** — generates and runs a `pptxgenjs` Node.js script that assembles the final `.pptx` with two-column layouts (text left, image right) for content slides

Slides that received a real PDF image skip NIM generation. Slides without one fall back to `image-gen-agent` (if running) or remain text-only.

**Task payload:**

```json
{
  "file_path": "C:/Users/you/Documents/report.pdf",
  "output_path": "C:/Users/you/Documents/report.pptx",
  "slide_count": 8,
  "theme": "dark executive",
  "nim_fallback": true,
  "min_img_width": 200,
  "min_img_height": 150
}
```

**Result:** `{ "pptx_path": "...", "slide_count": 8, "title": "...", "images_extracted": 5, "images_generated": 3, "error": null }`

---

## 15. Remote Nodes & Edge Deployment

Wactorz can run agents on any machine on your network — Raspberry Pi, VM, cloud server, or any device with Python 3.10+. The edge node only needs a single file and one pip package.

### How It Works

```
[Main machine]                        [Raspberry Pi / Edge node]
main_actor ──MQTT──► nodes/{name}/spawn ──► remote_runner.py
                                               │  compiles + runs agent
                                               │  heartbeats every 10s
dashboard  ◄──MQTT── agents/{id}/heartbeat ◄───┘
```

The `remote_runner.py` is fully self-contained — it reimplements the DynamicAgent contract inline without importing anything from the wactorz package. Remote agents appear in the dashboard and respond to MQTT commands exactly like local agents.

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

Or just ask in chat: _"spawn a temperature sensor agent on rpi-kitchen"_

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

Or via chat: _"move temp-sensor to rpi-bedroom"_

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
| `agent.publish(topic, data)` | YES | YES |
| `agent.log(msg)` / `agent.alert(msg)` | YES | YES |
| `agent.persist(key, val)` / `agent.recall(key)` | YES | YES (JSON file on the Pi) |
| `agent.send_to(name, payload)` | YES | YES (via MQTT round-trip) |
| `agent.node` | NO | YES (node name string) |
| `agent.llm.chat(prompt)` | YES | NO (no LLM provider on edge) |

For LLM reasoning from a remote agent, use `agent.send_to('main', {'text': prompt})` — main will call its LLM and return the result over MQTT.

### Installer Agent — Remote Actions

The installer agent handles three actions for node management:

| Action | Description |
|--------|-------------|
| `node_deploy` | Full bootstrap: upload runner + install aiomqtt + start process |
| `node_install` | Install pip packages on a running node via SSH |
| `node_run` | Run any shell command on a remote node via SSH |

All three accept `host`, `user`, and either `password` or `key_path` for SSH auth.

---

## 16. Installation & Configuration

### Quick Start

```bash
git clone https://github.com/waldiez/wactorz
cd wactorz
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
python -m wactorz
```

### MQTT Broker

Wactorz requires an MQTT broker. The simplest option is Mosquitto running locally:

```bash
# Windows (after installing Mosquitto)
mosquitto -v

# Docker
docker run -it -p 1883:1883 eclipse-mosquitto
```

By default Wactorz connects to `localhost:1883`. Override with `--mqtt-host` and `--mqtt-port`.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (primary LLM) |
| `OPENAI_API_KEY` | OpenAI key (alternative LLM) |
| `NIM_API_KEY` | NVIDIA NIM key (free tier — get at build.nvidia.com) |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Google Gemini API key (free tier — get at aistudio.google.com) |
| `HA_URL` / `HOME_ASSISTANT_URL` | Home Assistant base URL (e.g. `http://homeassistant.local:8123`) |
| `HA_TOKEN` / `HOME_ASSISTANT_TOKEN` | HA long-lived access token |
| `HA_MAP_AGENT_OUTPUT_TOPIC` | MQTT topic for `HomeAssistantMapAgent` (default: `homeassistant/map/entities_with_location`) |
| `HA_MAP_AGENT_TARGET_ACTOR` | Route map updates to a named actor instead of MQTT |
| `HA_STATE_BRIDGE_OUTPUT_TOPIC` | Base MQTT topic for `HomeAssistantStateBridgeAgent` (default: `homeassistant/state_changes`) |
| `HA_STATE_BRIDGE_DOMAINS` | Comma-separated domain allow-list for state bridge (e.g. `light,switch,sensor`; empty = all) |
| `HA_STATE_BRIDGE_PER_ENTITY` | `1` (default) = per-entity sub-topics; `0` = single shared topic |
| `DISCORD_BOT_TOKEN` | Discord bot token (for `--interface discord`) |
| `TWILIO_ACCOUNT_SID` | Twilio account SID (for `--interface whatsapp`) |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | Twilio WhatsApp sender number |

---

## 17. Troubleshooting

### Conversation history corruption (400 Bad Request loop)

If you see repeated `400` errors from the Anthropic API with `"Input should be a valid dictionary"`, the persisted conversation history has been corrupted. Run the included cleanup script once:

```bash
python fix_history.py
```

Then restart Wactorz. The LLM agent also sanitizes history on every load and before every API call as a belt-and-suspenders guard.

### Spawned agent takes too long to appear

Wactorz checks whether required packages are already importable before calling the installer. If a package is already installed, the agent spawns instantly. If the installer is called, it echoes the `task_id` back in its reply so the waiting future resolves immediately rather than sitting at the timeout.

### Pipeline rule set up but not triggering

1. Check `/rules` — verify all agents show green status
2. Check that `HomeAssistantStateBridgeAgent` is running (look for it in `/agents`)
3. Verify the entity ID is correct — run `@home-assistant-agent list_entities` to check
4. For HA state triggers the dynamic filter agent must be subscribed to the correct MQTT topic

### False "unresponsive" alerts for healthy agents

The monitor uses two liveness signals: `STATUS_RESPONSE` messages and `metrics.last_heartbeat` (updated every 10 seconds automatically). Infrastructure agents (monitor, installer, main, code-agent, anomaly-detector, home-assistant-agent) are excluded from user-facing notifications even if they are temporarily quiet.

### Discord bot not responding

Ensure **Message Content Intent** is enabled in the Discord Developer Portal (Bot → Privileged Gateway Intents). The bot only responds to messages prefixed with `!`.

---

## Appendix: File Structure

```
wactorz/
├── main.py                                    Entry point — CLI args, actor system setup, supervision tree
├── remote_runner.py                           Self-contained edge node runner — deploy to any Pi or machine
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
│   ├── llm_agent.py                           LLMAgent — 4 providers, rolling summarization, cost tracking
│   ├── main_actor.py                          MainActor — intent routing, memory, user facts, pipeline rules
│   ├── dynamic_agent.py                       DynamicAgent — runtime code executor, error events
│   ├── planner_agent.py                       PlannerAgent — task planning + reactive pipeline builder
│   ├── monitor_agent.py                       MonitorAgent — heartbeat, error registry, recovery
│   ├── installer_agent.py                     InstallerAgent — pip install locally + SSH deploy to remote nodes
│   ├── catalog_agent.py                       CatalogAgent — pre-built recipe library, spawns agents by name
│   ├── manual_agent.py                        ManualAgent — 3-layer PDF search and extraction
│   ├── home_assistant_agent.py                HomeAssistantAgent — HA automation CRUD (LLM-backed, intent routing)
│   ├── home_assistant_map_agent.py            HomeAssistantMapAgent — live entity/location map via HA WebSocket
│   ├── home_assistant_state_bridge_agent.py   HomeAssistantStateBridgeAgent — HA state_changed → MQTT bridge
│   ├── home_assistant_actuator_agent.py       HomeAssistantActuatorAgent — reactive MQTT→HA service actuator
│   ├── code_agent.py                          CodeAgent — sandboxed Python execution
│   └── ml_agent.py                            MLAgent, YOLOAgent, AnomalyDetectorAgent
│
└── interfaces/
    └── chat_interfaces.py                     CLI (with /deploy, /migrate, /nodes), REST, Discord, WhatsApp

catalogue_agents/                              Pre-built agent recipe files (loaded by CatalogAgent at startup)
├── __init__.py
├── image_gen_agent.py                         NIM FLUX.1-dev image generation
└── doc_to_pptx_agent.py                      PDF/TXT → PowerPoint conversion with real image extraction

state/                                         Persisted agent state (auto-created, never commit to git)
├── main/state.pkl                             Spawn registry, pipeline rules, user facts, webhook URLs, history
├── planner/state.pkl                          Plan cache
└── {agent-name}/state.pkl                     Per-agent persistent state
```

---

_Wactorz — the 24/7 agents built for the physical world._
