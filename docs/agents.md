# Agents

All agents are Python classes that extend `Actor`. They communicate exclusively via MQTT — no direct calls between agents at runtime.

---

## Core agents

Core agents are started by the Supervisor on launch and managed with `ONE_FOR_ONE` restart policies. They are **protected** — they cannot be stopped or deleted from the dashboard.

---

### MainActor `[core]` `[LLM]`

**File:** `wactorz/agents/main_actor.py`

| | |
|---|---|
| **name** | `main` |
| **restarts** | 10 |
| **persists** | `_spawned_agents`, `_pipeline_rules`, `_user_facts`, `_notification_urls`, `conversation_history`, `history_summary` → SQLite |

The LLM brain of the system. Every user message — from any interface — passes through MainActor. It classifies intent with a single LLM call (`ACTUATE` / `HA` / `PIPELINE` / `OTHER`), routes to the right agent, and streams replies back. Intent classification has a 60s timeout; if it expires, MainActor falls back to `OTHER`.

#### Intent routing

| Intent | Routed to | Example |
|--------|-----------|---------|
| `ACTUATE` | `OneOffActuatorAgent` (ephemeral) | "turn off the lamp" |
| `HA` | `home-assistant-agent` | "list all automations" |
| `PIPELINE` | a new `PlannerAgent` | "notify me on Discord when the door opens" |
| `OTHER` | `main.chat()` | "what's the weather like?" |
| `@mention` | named actor directly | `@my-agent {"action": "status"}` |

#### Memory

After every exchange, a background LLM task extracts durable facts (HA URLs, entity IDs, webhook URLs, preferences) into `_user_facts`, which are injected into the system prompt on the next call. Rolling summarisation kicks in when conversation history exceeds 30 messages. Both conversation history and user facts are stored in SQLite and survive restarts.

#### Spawn registry

Every DynamicAgent spawned during the session is saved to the `_spawned_agents` key in SQLite. On restart, MainActor re-spawns every entry so pipelines survive reboots. A fast-path import check skips dependency installation for packages that are already available, ensuring catalog agents restore instantly.

#### Commands

```
/agents                   — list all running agents with type and status
/agents <keyword>         — filter agents by capability keyword
/agents stop <name>       — stop and remove an agent
/rules                    — list active pipeline rules
/rules delete <id>        — stop agents and remove rule
/memory                   — show user facts and conversation summary
/memory clear             — wipe all memory
/memory forget <key>      — remove one fact
/webhook discord <url>    — store a Discord webhook URL
/webhook                  — list stored webhooks
/topics                   — list MQTT topics published by known agents
/nodes                    — list remote nodes and their agents
/bus                      — list TopicBus — Reactive Pub/Sub Registry
/help                     — show all commands
```

---

### PlannerAgent `[core]` `[LLM]`

**File:** `wactorz/agents/planner_agent.py`

| | |
|---|---|
| **name** | `planner-{hash}` (ephemeral) |
| **lifetime** | per-request |

Spawned by MainActor for every `PIPELINE`-classified request. The planner queries `home-assistant-agent` for the full list of real entity IDs, samples live topic schemas from the TopicBus, then asks the LLM to produce a multi-agent plan as a JSON array. Each step is one of three types: an `ha_actuator` agent (declarative HA service call), a `scheduled` agent (first-class time trigger — see [ScheduledAgent](#scheduledagent-spawned)), or a `dynamic` agent (Python code string). The planner spawns all agents, registers the pipeline rule with main, and exits.

After spawning, the planner fires a background `_bootstrap_ha_entity_states()` task that extracts HA entity IDs from the plan (generated code, `ha_actuator` actions, MQTT topics, and the enriched task string) and sends a `get_entities_state` request to `home-assistant-agent`. This re-publishes the current HA state over MQTT so freshly-spawned agents that subscribe to `homeassistant/state_changes/#` fire immediately — without waiting for the next real HA state change.

#### Supported patterns

| Pattern | Trigger | Action | Agents spawned |
|---------|---------|--------|----------------|
| 1 | HA sensor state change | HA service call | dynamic filter + `ha_actuator` |
| 2 | HA sensor state change | Discord/webhook notification | dynamic agent |
| 3 | Webcam detection (YOLO) | HA service call | dynamic YOLO + `ha_actuator` |
| 4 | Webcam detection (YOLO) | Discord/webhook notification | dynamic YOLO + dynamic notify |
| 5 | Timer/schedule | HA service call OR notification | `scheduled` + `ha_actuator` (or `scheduled` + `dynamic` notify) |
| 6 | MQTT sensor + condition | HA service call | dynamic monitor + `ha_actuator` |

#### Code validation

After the LLM generates code, `_validate_pipeline_code()` scans each dynamic agent's code for common mistakes: strips `await` from synchronous agent methods, rewrites raw `aiomqtt.Client()` usage to `agent.subscribe()`, and flags direct HA REST API calls that should use `ha_actuator` instead.

#### TopicBus integration

Before generating code, the planner calls `prune_stale()` on the TopicBus registry to remove contracts from stopped agents, then reads `observed_samples` to inject real field names into the LLM prompt. This solves the vocabulary mismatch problem — the LLM writes `payload["temp"]` instead of guessing `payload["temperature"]`.

> **ℹ MQTT topic rule** — All generated agents always subscribe to `homeassistant/state_changes/#` (wildcard) and filter by `entity_id` in the payload — never by topic path. This works regardless of the `HA_STATE_BRIDGE_PER_ENTITY` setting.

---

### MonitorAgent `[core]`

**File:** `wactorz/agents/monitor_agent.py`

| | |
|---|---|
| **name** | `monitor` |
| **check interval** | 15 s |
| **heartbeat timeout** | 60 s |

Tracks heartbeat timestamps from every registered actor. If an actor's last heartbeat is older than `heartbeat_timeout` seconds it publishes an alert to `agents/{monitor_id}/alert` and notifies MainActor directly. Does _not_ auto-restart actors — restart policy belongs to the Supervisor. Infrastructure agents (`monitor`, `installer`, `main`, `home-assistant-agent`, `code-agent`, `anomaly-detector`) are excluded from user-facing notifications.

---

### IOAgent `[core]`

**File:** `wactorz/agents/io_agent.py`

| | |
|---|---|
| **name** | `io-agent` |
| **subscribes** | `io/chat` |

MQTT ↔ interface gateway. Bridges incoming messages from the web dashboard to MainActor and relays responses back. The CLI, Discord, WhatsApp, and Telegram interfaces bypass IOAgent and call `main_actor.process_user_input()` directly.

---

### InstallerAgent `[core]`

**File:** `wactorz/agents/installer_agent.py`

| | |
|---|---|
| **name** | `installer` |
| **restarts** | 3 |

Runs `pip install` in a subprocess on request. Called automatically by `CatalogAgent` before spawning a recipe that declares an `"install": [...]` list. Also handles remote node deployment via SSH (`node_deploy`, `node_install`, `node_run` actions). Replies with a result dict so the caller can gate on success before proceeding.

```
@installer {"action": "install", "packages": ["httpx", "aiomqtt"]}
```

---

### CatalogAgent `[core]` `[catalog]`

**File:** `wactorz/agents/catalog_agent.py`

| | |
|---|---|
| **name** | `catalog` |
| **restarts** | 10 |
| **recipes dir** | `wactorz/catalogue_agents/` |

Pre-built agent recipe library. On startup it loads every `AGENT_CODE` string from `wactorz/catalogue_agents/*.py` and injects a manifest for each recipe into MainActor so the LLM is aware of what can be spawned. When asked to spawn a recipe it first asks InstallerAgent to install any declared dependencies, then creates a DynamicAgent with the recipe code and `trusted=True` — bypassing the code safety validator since catalog agents are pre-built and tested.

#### Usage

```
@catalog list
@catalog info anomaly-detector
@catalog spawn anomaly-detector
```

See the [Catalog recipes](#catalog-recipes) section below for available recipes.

---

### HomeAssistantAgent `[core]` `[LLM]`

**File:** `wactorz/agents/home_assistant_agent.py`

| | |
|---|---|
| **name** | `home-assistant-agent` |
| **restarts** | 5 |

Wraps the Home Assistant REST API. Uses multiple internal LLM calls to classify the user's intent and generate the appropriate API call.

#### Supported actions

| Action | Description |
|--------|-------------|
| `list_entities` | Fetch all entities (used by Planner and OneOffActuator) |
| `list_automations` | List all automations |
| `list_areas` | List all areas |
| `list_devices` | List all devices |
| `recommend_hardware` | Hardware recommendations using compact `get_simplified_ha_data` snapshot |
| `create_automation` | **Temporarily disabled** — currently routed to `recommend_hardware` instead of generating an automation |
| `edit_automation` | Identify and update an existing automation |
| `delete_automation` | Remove an automation |
| `get_entities_state` | Fetch current state for explicit entity IDs and re-publish to MQTT — used by PlannerAgent bootstrap |
| `other` | Answer open-ended HA questions via a short LLM tool-call loop backed by `get_simplified_ha_data` |

#### Prompts

All LLM system prompts used by this agent live in `wactorz/agents/prompts/home_assistant_prompts.py`.

#### Configuration

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=eyJ...   # Long-lived access token
```

---

### OneOffActuatorAgent `[core]`

**File:** `wactorz/agents/one_off_actuator_agent.py`

| | |
|---|---|
| **name** | `one-off-actuator-{hash}` (ephemeral) |
| **lifetime** | single request |

Spawned by MainActor for `ACTUATE` intent requests — immediate one-shot device control. Before spawning, MainActor fetches the full HA entity list via `home-assistant-agent` and appends it to the request text so the actuator's LLM can resolve natural language device names ("the lamp") to specific entity IDs (`light.wiz_rgbw_tunable_02cba0`).

The agent resolves the request to HA service calls, executes them via the HA WebSocket API, sends the result back to MainActor, then deletes itself. The resolver LLM call and the one-shot actuation wait both allow up to 120s, which gives local Ollama models enough time to respond without prematurely timing out.

Examples: "turn on the living room light", "set heating to 23 degrees", "lock the front door".

---

### HomeAssistantActuatorAgent `[core]`

**File:** `wactorz/agents/home_assistant_actuator_agent.py`

| | |
|---|---|
| **name** | set at spawn time |
| **spawned by** | PlannerAgent |

The reactive action end of every HA pipeline. Subscribes to one or more MQTT topics, evaluates an optional detection filter and HA entity conditions, enforces a configurable cooldown, and calls HA services via a persistent WebSocket connection.

```
DynamicAgent (sensor/filter) → MQTT topic → HomeAssistantActuatorAgent → HA service call
```

Configured with an `ActuatorConfig` specifying `mqtt_topics`, `detection_filter`, `conditions`, `actions`, and `cooldown_seconds`. Detection filter supports equality and operator dicts (`{"gte": 0.7}`).

---

### HomeAssistantStateBridgeAgent `[core]`

**File:** `wactorz/agents/home_assistant_state_bridge_agent.py`

| | |
|---|---|
| **name** | `home-assistant-state-bridge` |
| **restarts** | 5 |

Subscribes to the Home Assistant WebSocket API and republishes every state-change event to MQTT so pipeline agents can react to device events without polling HA directly.

#### Topic format

| Config | Topic published |
|--------|-----------------|
| `HA_STATE_BRIDGE_PER_ENTITY=0` (default) | `homeassistant/state_changes` — all entities on one flat topic |
| `HA_STATE_BRIDGE_PER_ENTITY=1` | `homeassistant/state_changes/{domain}/{entity_id}` |

> **⚠ Always use the wildcard** — Subscribe to `homeassistant/state_changes/#` and filter by `entity_id` in the payload. Never hardcode the per-entity topic path — it may not exist depending on configuration.

#### Payload

```json
{
  "entity_id": "light.wiz_rgbw_tunable_02cba0",
  "domain":    "light",
  "new_state": {"state": "on", "attributes": {}},
  "old_state": {"state": "off", "attributes": {}}
}
```

---

### HomeAssistantMapAgent `[core]`

**File:** `wactorz/agents/home_assistant_map_agent.py`

| | |
|---|---|
| **name** | `home-assistant-map-agent` |
| **restarts** | 5 |

Maintains a live map of entity IDs to friendly names and domains. Used by PlannerAgent and OneOffActuatorAgent to resolve user-friendly device names ("the living room lamp") to actual HA entity IDs before generating pipeline code or executing service calls.

---

### ScheduledAgent `[spawned]`

**File:** `wactorz/agents/scheduled_agent.py`

| | |
|---|---|
| **name** | set at spawn time (e.g. `evening-lights-trigger`) |
| **spawned by** | PlannerAgent (pattern 5 — scheduled trigger) |
| **persists** | `_schedule_state` (last fire time, fire count) → SQLite |

First-class scheduled trigger primitive. Sleeps until the next fire time, publishes to its topic, then loops. Replaces the broken `while True: if datetime.now()…` patterns that LLM-generated dynamic agents kept producing for time-based rules.

#### Schedule spec

```python
{"type": "daily",    "at": "17:00"}
{"type": "weekly",   "at": "07:00", "days": ["mon", "tue", "wed", "thu", "fri"]}
{"type": "interval", "seconds": 1800}
{"type": "once",     "at": "2026-05-02T17:00:00"}    # ISO-8601 local time
{"type": "cron",     "expr": "0 17 * * *"}            # escape hatch
```

Day names: `mon|tue|wed|thu|fri|sat|sun` (full names also accepted).

#### Fire topic

Publishes to `schedule/{name}/fired` with payload:

```json
{"fired_at": "<ISO-8601 UTC>", "schedule_type": "daily", "agent": "evening-lights-trigger"}
```

A downstream `ha_actuator` or `dynamic` agent subscribes to this topic and performs the actual action.

#### Timezone resolution

1. Schedule spec's optional `"tz"` field (e.g. `"Europe/Athens"`)
2. `timezone` kwarg injected by MainActor from the user's `pref_timezone` fact
3. System local timezone

#### Catch-up behaviour on restart

- **`once` schedules** — fire if missed within the last 5 minutes; otherwise the agent self-deletes silently.
- **Recurring schedules** — never catch up. Resume from the next fire time.

The internal sleep is bounded to 5 minutes so DST transitions, system clock jumps, and laptop sleep don't strand the agent.

---

### TimeSeriesCollector `[core]`

**File:** `wactorz/agents/timeseries_collector.py`

| | |
|---|---|
| **name** | `timeseries-collector` |
| **restarts** | 5 |
| **persists** | `sensor_readings`, `detections`, `ha_state_changes`, `actuations` → SQLite |

Background data collector that subscribes to MQTT topics and writes every message to SQLite time-series tables. No LLM involved — pure append-only data collection for historical queries and ML training.

#### Subscriptions

| Topic pattern | Data type |
|---------------|-----------|
| `sensors/#` | Sensor readings (temp, humidity, energy, etc.) |
| `custom/sensors/#` | Custom agent sensor data |
| `custom/detections/#` | YOLO/camera detection events |
| `homeassistant/state_changes/#` | HA state change events |
| `sinergym/env/+/observation` | Sinergym simulation step data |
| `sinergym/env/+/episode` | Sinergym episode start/end events |

#### Write strategy

Messages are buffered in memory and flushed to SQLite every `batch_interval` seconds (default: 5s) or when the buffer reaches `batch_size` (default: 200 messages). A final flush runs on agent stop. Data older than `retention_days` (default: 90) is pruned automatically every 6 hours.

#### Sinergym data handling

Sinergym observations are flattened into individual field rows: each `obs_i` dimension, each `action_i` dimension, `reward`, `step`, `episode`, and every numeric field from the `info` dict (`info_total_power_demand`, `info_total_temperature_violation`, etc.). Episode end events are stored as summary rows (`ep_total_reward`, `ep_mean_reward`, `ep_steps`, etc.).

#### Commands

```
@timeseries-collector stats     — show received/written counts and table row counts
@timeseries-collector prune     — force immediate data pruning
```

#### Configuration (environment variables)

```bash
TS_RETENTION_DAYS=90       # auto-prune data older than this (default: 90)
TS_BATCH_INTERVAL=5.0      # flush to SQLite every N seconds (default: 5)
```

---

### FusekiAgent `[optional]`

**File:** `wactorz/agents/fuseki_agent.py`

| | |
|---|---|
| **name** | `fern-agent` |
| **protected** | `false` — can be stopped/deleted from the dashboard |

SPARQL interface to Apache Jena Fuseki. Executes `SELECT`, `CONSTRUCT`, `DESCRIBE`, and `ASK` queries against the configured triplestore. No LLM involved — pure graph query agent.

#### Configuration

```bash
FUSEKI_URL=http://localhost:3030   # default
FUSEKI_DATASET=wactorz             # default
```

#### Commands

```
@fern-agent query SELECT * WHERE { ?s ?p ?o } LIMIT 5
@fern-agent ask ASK { <http://example.org/foo> a owl:Class }
@fern-agent prefixes           — list common RDF prefix bindings
@fern-agent datasets           — list available Fuseki datasets
```

The Wactorz ontology (`infra/fuseki/ontology/wactorz.ttl`) models the running agent topology as RDF: each agent is an `af:Agent` with `af:publishesTo` / `af:subscribesTo` links to `af:Channel` nodes. Live agent metrics (`messagesProcessed`, `errorsCount`, `costUsd`, etc.) are updated continuously by `MetricsBridge`, which subscribes to `agents/+/metrics` MQTT and writes each heartbeat payload to Fuseki via `FusekiClient.upsert_agent_metrics()`.

---

## DynamicAgent

**File:** `wactorz/agents/dynamic_agent.py`

| | |
|---|---|
| **name** | set at spawn time |
| **spawned by** | MainActor, PlannerAgent, CatalogAgent |

The core extensibility primitive. DynamicAgent compiles and runs a Python code string at runtime — the LLM writes the code, Wactorz executes it. Three optional functions can be defined:

| Function | When called | Notes |
|----------|-------------|-------|
| `async def setup(agent)` | Once at start | Always runs as a background `asyncio.create_task` — never blocks the heartbeat loop. Long-running MQTT subscription loops go here. |
| `async def process(agent)` | Every `poll_interval` seconds | For periodic work. Timeout: 120s per call. Only started after `setup()` returns (or immediately if no setup). |
| `async def handle_task(agent, payload)` | On every inbound TASK message | Must return a dict. Timeout: 60s per call. Used for command/response interactions. |

#### Agent API (`agent` object inside generated code)

| Method | Sync/Async | Description |
|--------|------------|-------------|
| `await agent.publish(topic, payload)` | async | Publish a dict to any MQTT topic |
| `agent.subscribe(topic, callback)` | **sync** | Subscribe to MQTT topic; callback is `async def cb(payload)`. Returns immediately. |
| `await agent.send_to(name, payload)` | async | Send a TASK message to another actor by name |
| `await agent.log(message)` | async | Publish to the agent's log stream |
| `await agent.alert(message, level)` | async | Publish an alert (`info`, `warning`, `error`) |
| `agent.persist(key, value)` | **sync** | Write a value (routes to SQLite/Redis/Pickle based on key) |
| `agent.recall(key)` | **sync** | Read a persisted value |
| `agent.state` | — | In-memory dict (not persisted) |
| `agent.window(topic, seconds)` | **sync** | Create a sliding time window over an MQTT topic stream |
| `agent.declare_contract(...)` | **sync** | Register a TopicBus contract for auto-wiring |
| `agent.query_ts(hours, field, ...)` | **sync** | Query historical sensor data from SQLite |
| `agent.query_detections(hours, ...)` | **sync** | Query historical YOLO detections from SQLite |
| `agent.query_ha_states(hours, ...)` | **sync** | Query historical HA state changes from SQLite |
| `agent.ts_stats()` | **sync** | Row counts for all time-series tables |
| `agent.agents()` | **sync** | List all currently running agents |
| `agent.topics(keyword)` | **sync** | List known MQTT topics |

#### Time-series queries

Any dynamic agent can query historical data collected by the TimeSeriesCollector:

```python
async def handle_task(agent, payload):
    # Get last 24h of temperature readings
    rows = agent.query_ts(hours=24, field='temp')

    # Get as pandas DataFrame for ML training
    df = agent.query_ts(hours=168, entity_id='sensor.kitchen_temp', as_dataframe=True)

    # Query YOLO detections
    df = agent.query_detections(hours=12, class_name='person', as_dataframe=True)

    # Check available data volume
    stats = agent.ts_stats()
    # {'sensor_readings': 145230, 'detections': 8920, ...}
```

#### Code safety

LLM-generated agents go through a 5-layer defense:

1. **Prompt engineering** — LLM prompt lists sync vs async methods
2. **Code sanitizer** — regex strips `await` from sync methods, removes LLM self-setup blocks
3. **Safety validator** — blocks `os.system`, `eval`, `__import__`, file writes, raw sockets
4. **Callback wrapper** — catches `TypeError` from accidental `await None` in subscribe callbacks
5. **LLM self-correction** — if `setup()` crashes, traceback is sent to LLM for fix (2 attempts)

Catalog agents spawn with `trusted=True` and bypass layers 2-3 (sanitizer + safety validator), since their code is pre-built and may legitimately use `__import__`, `subprocess`, etc.

#### Example — MQTT subscription

```python
async def setup(agent):
    async def on_state(payload):
        if payload.get("entity_id") != "light.my_lamp":
            return
        if payload.get("new_state", {}).get("state") == "on":
            import httpx
            async with httpx.AsyncClient() as c:
                await c.post(
                    "https://discord.com/api/webhooks/...",
                    json={"content": "Lamp turned on!"}
                )
            await agent.log("Discord notification sent")

    agent.subscribe("homeassistant/state_changes/#", on_state)
```

#### Example — periodic polling

```python
async def setup(agent):
    agent.state["count"] = int(agent.recall("count") or 0)

async def process(agent):
    agent.state["count"] += 1
    agent.persist("count", agent.state["count"])
    await agent.publish("custom/counter", {"count": agent.state["count"]})
```

---

## LLMAgent base class

**File:** `wactorz/agents/llm_agent.py`

Base class for all LLM-backed agents. Manages conversation history, rolling summarisation (compresses the oldest half of history when the message count exceeds `summarize_threshold=30`), and cost tracking. Conversation history and summary are persisted to SQLite and restored on startup.

#### Providers

| Class | Flag | Env var | Notes |
|-------|------|---------|-------|
| `AnthropicProvider` | `--llm anthropic` | `ANTHROPIC_API_KEY` | Default. Streaming supported. |
| `OpenAIProvider` | `--llm openai` | `OPENAI_API_KEY` | Any OpenAI-compatible endpoint. |
| `OllamaProvider` | `--llm ollama --ollama-model llama3` | — | Local. No cost tracking. |
| `NIMProvider` | `--llm nim --nim-model meta/llama-3.3-70b-instruct` | `NIM_API_KEY` | NVIDIA NIM. Free tier: 1000 req/month per model. |
| `GeminiProvider` | `--llm gemini --gemini-model gemini-2.5-flash` | `GEMINI_API_KEY` | Google Gemini via `google-generativeai` SDK. Free tier available. |

All providers receive the same `complete(messages, system)` and `stream(messages, system)` calls. Ollama sends the system prompt as the first `{"role": "system"}` message in the native `/api/chat` payload, so local models receive the same persona/instructions as hosted providers.

#### Cost tracking

All providers track token usage and compute cost in USD per call. Costs are accumulated in `LLMAgent.metrics` and published with every heartbeat.

Pricing is resolved dynamically: on startup, `LLMAgent` fetches live per-token rates from the [LiteLLM model catalogue](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and caches them for 24 hours. If the fetch fails or the model isn't listed, it falls back to the hardcoded `_FALLBACK_PRICING` table in `llm_agent.py`. To debug which source a model is using, call `pricing_info(model_name)` — it returns `source` (`"live"` or `"fallback"`), rates, and cache age.

#### Spend limit

A hard cap on LLM API spend can be set per period. When the limit is reached, further LLM calls are blocked and the agent replies with a "limit reached" message instead of calling the API.

Configure via env vars (startup default):

```
LLM_COST_LIMIT_USD=0.70
LLM_COST_LIMIT_PERIOD=daily   # daily | weekly | monthly | total
```

The limit can also be updated at runtime from the dashboard Settings tab or via `POST /api/cost/limit` — the runtime value persists in SQLite and takes priority over the env var. Spend resets automatically at period boundaries (midnight UTC for daily, Monday for weekly, first of the month for monthly). To reset manually, call `POST /api/cost/reset`.

The dashboard COST tile shows a progress bar: yellow at 80% of the limit, red at 100%.

See [API reference](api.md#cost-management) for the full endpoint spec.

---

## Catalog recipes

Recipes live in `wactorz/catalogue_agents/` as plain Python files exporting an `AGENT_CODE` string. They are loaded by `CatalogAgent` at startup and spawned on demand as DynamicAgents with `trusted=True` (safety validator bypassed).

| Recipe name | File | Description | Deps |
|-------------|------|-------------|------|
| `image-gen-agent` | `image_gen_agent.py` | Generates images from text prompts using NVIDIA NIM FLUX.1-dev. Returns the absolute path to the saved PNG. | `requests` |
| `doc-to-pptx-agent` | `doc_to_pptx_agent.py` | Converts PDF or TXT documents into PowerPoint presentations. Extracts embedded images from PDF; optionally uses NIM FLUX for slides without images. | `pymupdf`, `pdfplumber`, `pillow` |
| `sinergym-collector` | `sinergym_collector_agent.py` | Collects Sinergym episode data via MQTT for RL/Bayesian training. Listens on `sinergym/env/{env_id}/observation`, buffers transitions per-episode, persists episode blobs, and signals the optimizer on collection complete. | `aiomqtt`, `numpy` |
| `sinergym-optimizer` | `sinergym_optimizer_agent.py` | Env-aware GP-UCB Q(s,a) optimizer with RBC warm-start. Trains from collected episodes (RL PPO/SAC or Bayesian GP), then publishes actions to `sinergym/env/{env_id}/action` during deployment. Auto-introspects obs/action variable names and comfort models. | `stable-baselines3`, `scikit-learn`, `numpy`, `torch`, `aiomqtt`, `gymnasium` |
| `anomaly-detector` | `anomaly_detector_agent.py` | Learns normal patterns from time-series data (HA sensors and Sinergym), detects anomalies in real-time. Statistical z-score, percentile range, rate-of-change, and absence detection. Works with both real-world HA devices and simulated building data. | `aiomqtt`, `numpy` |
| `manual-agent` | `manual_agent.py` | Searches the web for device manuals, downloads PDFs, extracts text, and answers questions about them using the agent's LLM. | `httpx`, `pdfplumber`, `duckduckgo_search` |

> **💡 Adding a recipe** — Create `wactorz/catalogue_agents/my_agent.py` exporting `AGENT_CODE = r'''...'''`, then add an entry to `_build_catalog()` in `wactorz/agents/catalog_agent.py`. The recipe is available on the next restart without any other changes.

---

## Writing a new core agent

For agents that need to be part of the supervision tree (always running, not spawnable from chat), subclass `Actor` directly:

```python
from wactorz.core.actor import Actor, Message, MessageType

class MyAgent(Actor):

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "my-agent")
        super().__init__(**kwargs)

    async def on_start(self):
        asyncio.create_task(self._my_loop())

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        result = {"echo": msg.payload}
        if isinstance(msg.payload, dict):
            result["_task_id"] = msg.payload.get("_task_id")
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, result)

    async def _my_loop(self):
        while True:
            await self._mqtt_publish("custom/my-agent/tick", {"ts": time.time()})
            await asyncio.sleep(10)
```

Then register it in `cli.py` inside `build_system()`:

```python
from wactorz.agents.my_agent import MyAgent

def make_my_agent():
    return _wire_persistence(
        MyAgent(name="my-agent", persistence_dir="./state"))

system.supervisor.supervise(
    "my-agent", make_my_agent,
    strategy=SupervisorStrategy.ONE_FOR_ONE,
    max_restarts=5, restart_delay=1.0
)
```
