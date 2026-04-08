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
| **persists** | `state/main/state.pkl` |

The LLM brain of the system. Every user message — from any interface — passes through MainActor. It classifies intent with a single LLM call (`HA` / `PIPELINE` / `OTHER`), routes to the right agent, and streams replies back.

#### Intent routing

| Intent | Routed to | Example |
|--------|-----------|---------|
| `HA` | `home-assistant-agent` | "turn off the living room lights" |
| `PIPELINE` | a new `PlannerAgent` | "notify me on Discord when the door opens" |
| `OTHER` | `main.chat()` | "what's the weather like?" |
| `@mention` | named actor directly | `@my-agent {"action": "status"}` |

#### Memory

After every exchange, a background LLM task extracts durable facts (HA URLs, entity IDs, webhook URLs, preferences) into `_user_facts`, which are injected into the system prompt on the next call. Rolling summarisation kicks in when conversation history exceeds 30 messages.

#### Spawn registry

Every DynamicAgent spawned during the session is saved to `state/main/state.pkl` under `_spawned_agents`. On restart, MainActor re-spawns every entry so pipelines survive reboots.

#### Commands

```
/rules                    — list active pipeline rules
/rules delete <id>        — stop agents and remove rule
/memory                   — show user facts and conversation summary
/memory clear             — wipe all memory
/memory forget <key>      — remove one fact
/webhook discord <url>    — store a Discord webhook URL
/webhook                  — list stored webhooks
```

---

### PlannerAgent `[core]` `[LLM]`

**File:** `wactorz/agents/planner_agent.py`

| | |
|---|---|
| **name** | `planner-{hash}` (ephemeral) |
| **lifetime** | per-request |

Spawned by MainActor for every `PIPELINE`-classified request. The planner queries `home-assistant-agent` for the full list of real entity IDs, then asks the LLM to produce a multi-agent plan as a JSON array. Each step is either a `dynamic` agent (Python code string) or an `ha_actuator` agent (declarative HA service call). The planner spawns all agents, registers the pipeline rule with main, and exits.

#### Supported patterns

- **HA sensor → HA action** — e.g. motion sensor turns on light
- **HA sensor → notification** — e.g. door opens → Discord message
- **Webcam detection → HA action** — e.g. person detected → unlock door
- **Webcam detection → notification** — e.g. cat detected → Telegram photo
- **Timer → HA action** — e.g. every day at 07:00 turn on coffee maker

> **ℹ MQTT topic rule** — All generated agents always subscribe to `homeassistant/state_changes/#` (wildcard) and filter by `entity_id` in the payload — never by topic path. This works regardless of the `HA_STATE_BRIDGE_PER_ENTITY` setting.

---

### MonitorAgent `[core]`

**File:** `wactorz/agents/monitor_agent.py`

| | |
|---|---|
| **name** | `monitor` |
| **check interval** | 15 s |
| **heartbeat timeout** | 60 s |

Tracks heartbeat timestamps from every registered actor. If an actor's last heartbeat is older than `heartbeat_timeout` seconds it publishes an alert to `agents/{monitor_id}/alert` and notifies MainActor directly. Does _not_ auto-restart actors — restart policy belongs to the Supervisor.

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

Runs `pip install` in a subprocess on request. Called automatically by `CatalogAgent` before spawning a recipe that declares an `"install": [...]` list. Replies with a result dict so the caller can gate on success before proceeding.

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
| **recipes dir** | `catalogue_agents/` |

Pre-built agent recipe library. On startup it loads every `AGENT_CODE` string from `catalogue_agents/*.py` and injects a manifest for each recipe into MainActor so the LLM is aware of what can be spawned. When asked to spawn a recipe it first asks InstallerAgent to install any declared dependencies, then creates a DynamicAgent with the recipe code.

#### Usage

```
@catalog list
@catalog info discord-notify-agent
@catalog spawn discord-notify-agent
@catalog spawn ha-actuator-agent
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
| `list_entities` | Fetch all entities (used by Planner) |
| `call_service` | Turn on/off lights, switches, climate, covers, scripts… |
| `create_automation` | Generate and POST a YAML automation via HA API |
| `delete_entity` | Remove an entity or automation |
| `get_state` | Read current state of one or more entities |

#### Configuration

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=eyJ...   # Long-lived access token
```

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

Maintains a live map of entity IDs to friendly names and domains. Used by PlannerAgent to resolve user-friendly device names ("the living room lamp") to actual HA entity IDs before generating pipeline code.

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
| `async def process(agent)` | Every `poll_interval` seconds | For periodic work. Only started after `setup()` returns (or immediately if no setup). |
| `async def handle_task(agent, payload)` | On every inbound TASK message | Must return a dict. Used for command/response interactions. |

#### Agent API (`agent` object inside generated code)

| Method | Description |
|--------|-------------|
| `await agent.publish(topic, payload)` | Publish a dict to any MQTT topic |
| `agent.subscribe(topic, callback)` | Subscribe to MQTT topic; callback is `async def cb(payload: dict)`. Runs as background task — `setup()` returns immediately. |
| `await agent.send_to(name, payload)` | Send a TASK message to another actor by name |
| `await agent.log(message)` | Publish to the agent's log stream (visible in dashboard) |
| `await agent.alert(message, level)` | Publish an alert. Levels: `info`, `warning`, `error` |
| `agent.persist(key, value)` | Write a value to the agent's pickle state (survives restarts) |
| `agent.recall(key)` | Read a persisted value |
| `agent.state` | In-memory dict (not persisted) |

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

    # Wildcard subscription — works with any HA_STATE_BRIDGE_PER_ENTITY setting
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

Base class for all LLM-backed agents. Manages conversation history, rolling summarisation (compresses the oldest half of history when the message count exceeds `summarize_threshold=30`), and cost tracking.

#### Providers

| Class | Flag | Env var | Notes |
|-------|------|---------|-------|
| `AnthropicProvider` | `--llm anthropic` | `ANTHROPIC_API_KEY` | Default. Streaming supported. |
| `OpenAIProvider` | `--llm openai` | `OPENAI_API_KEY` | Any OpenAI-compatible endpoint via `--openai-base-url`. |
| `OllamaProvider` | `--llm ollama --ollama-model llama3` | — | Local. No cost tracking. |
| `NIMProvider` | `--llm nim --nim-model meta/llama-3.3-70b-instruct` | `NIM_API_KEY` | NVIDIA NIM. Free tier: 1000 req/month per model. |
| `GeminiProvider` | `--llm gemini --gemini-model gemini-2.5-flash` | `GEMINI_API_KEY` | Google Gemini via `google-generativeai` SDK. Free tier available. |

#### Cost tracking

All providers track token usage and compute cost in USD per call. Costs are accumulated in `LLMAgent.metrics` and published with every heartbeat. The `PRICING` dict in `llm_agent.py` covers all major model variants — add new entries there to track custom models.

---

## Catalog recipes

Recipes live in `catalogue_agents/` as plain Python files exporting an `AGENT_CODE` string. They are loaded by `CatalogAgent` at startup and spawned on demand as DynamicAgents.

| Recipe name | File | Description | Deps |
|-------------|------|-------------|------|
| `discord-notify-agent` | `discord_notify_agent.py` | Subscribes to any MQTT topic and posts a message to a Discord webhook when a triggering event arrives. Configurable cooldown, trigger key/value filter, and message template. | `aiohttp`, `aiomqtt` |
| `ha-actuator-agent` | `ha_actuator_agent.py` | Subscribes to an MQTT topic and calls a Home Assistant service when a detection filter matches the payload. Used as the action side of HA pipelines. | `aiomqtt` |
| `image-gen-agent` | `image_gen_agent.py` | Generates images from text prompts using NVIDIA NIM FLUX.1-dev. Returns the absolute path to the saved PNG. | `requests` |
| `doc-to-pptx-agent` | `doc_to_pptx_agent.py` | Converts PDF or TXT documents into PowerPoint presentations. Extracts embedded images from PDF; optionally uses NIM FLUX for slides without images. | `pymupdf`, `pdfplumber`, `pillow` |

> **💡 Adding a recipe** — Create `catalogue_agents/my_agent.py` exporting `AGENT_CODE = r'''...'''`, then add an entry to `_build_catalog()` in `catalog_agent.py`. The recipe is available on the next restart without any other changes.

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
        # Long-running work must be a background task
        asyncio.create_task(self._my_loop())

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        result = {"echo": msg.payload}
        # Echo _task_id so caller futures resolve
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
    return MyAgent(name="my-agent", persistence_dir="./state")

system.supervisor.supervise(
    "my-agent", make_my_agent,
    strategy=SupervisorStrategy.ONE_FOR_ONE,
    max_restarts=5, restart_delay=1.0
)
```