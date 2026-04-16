# Architecture

Wactorz is a Python-first asyncio actor-model framework for building multi-agent AI systems. Agents communicate via MQTT and run in a single process — start everything with one command.

## Overview

The framework is built on three ideas: every agent is an independent **actor** with its own message loop; **MQTT** is the universal communication bus (between agents, to the dashboard, and to external systems); and **DynamicAgent** allows the LLM to write and spawn new agents at runtime without restarting the process.

```
┌─────────────────────────────────────────────────────────────────┐
│  User interfaces                                                 │
│  CLI  ·  REST  ·  Discord  ·  WhatsApp  ·  Telegram             │
│  Web UI (port 8888)                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │  asyncio  (single Python process)
                         ▼
┌────────────────────────────────────────────────────────────────────────┐
│  ActorSystem  (wactorz/core/registry.py)                               │
│                                                                        │
│  Supervisor  ──  ONE_FOR_ONE restart strategy per actor                │
│  ActorRegistry  ──  name → actor lookup, MQTT publisher                │
│                                                                        │
│  MainActor           LLM orchestrator, intent routing, spawn registry  │
│  MonitorAgent        heartbeat watchdog, alerts main on failure        │
│  IOAgent             MQTT↔UI gateway, routes chat to main             │
│  CatalogAgent        pre-built recipe library, spawns on request       │
│  InstallerAgent      pip installs deps for dynamic agents              │
│  HomeAssistantAgent  HA REST API — entities, services, automations     │
│  PlannerAgent        spawned per-request, builds multi-agent pipelines │
│  DynamicAgent*       LLM-generated Python, spawned at runtime         │
└────────────────────────────┬───────────────────────────────────────────┘
                             │  pub / sub
                             ▼
                    MQTT broker  (embedded, starts with wactorz)
                    :1883 TCP  (default)
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
   Home Assistant              External systems via MQTT
   WebSocket API               custom topics, any device or service
```

> **💡 One command to start** — Running `wactorz` starts everything — the actor system, and the web dashboard. Note a separate MQTT broker process needed.

---

## Components

### Core — `wactorz/core/`

| File | Responsibility |
|------|----------------|
| `actor.py` | `Actor` base class — message loop, heartbeat, persistence (`pickle`), supervisor strategy enum |
| `registry.py` | `ActorSystem`, `ActorRegistry`, `Supervisor`, `_MQTTPublisher` (shared aiomqtt connection) |

### Built-in Agents — `wactorz/agents/`

See the [Agents reference](agents.md) for full documentation. A summary of the core actors:

| Agent | Role |
|-------|------|
| **MainActor** | LLM orchestrator. Classifies intent, routes messages, manages the spawn registry and user memory. |
| **PlannerAgent** | Spawned per pipeline request. Generates a multi-agent plan and spawns the required DynamicAgents. |
| **DynamicAgent** | Executes LLM-generated Python at runtime from a `setup()` / `process()` / `handle_task()` code string. |
| **CatalogAgent** | Pre-built recipe library. Loads agents from `catalogue_agents/` and spawns them on request. |
| **HomeAssistantAgent** | Wraps the HA REST API — entities, services, automations. Uses internal LLM calls for classification. |
| **HomeAssistantStateBridgeAgent** | Streams HA state changes to MQTT so pipeline agents can react to device events in real time. |
| **MonitorAgent** | Tracks heartbeats from all actors. Alerts main when an agent is unresponsive for >60 s. |
| **InstallerAgent** | Runs `pip install` in a subprocess. Called automatically before spawning a recipe with declared dependencies. |
| **LLMAgent** | Base class for LLM-backed agents. Handles conversation history, rolling summarisation, and cost tracking across all providers. |

---

## Actor Lifecycle

Every actor in Wactorz follows the same asyncio lifecycle:

```python
class MyAgent(Actor):
    async def on_start(self):
        # Called once when the actor starts.
        # Long-running work (MQTT subscriptions, polling loops)
        # must be launched as asyncio.create_task() — never awaited directly.
        asyncio.create_task(self._my_loop())

    async def handle_message(self, msg: Message):
        # Called for every message in the inbox.
        # msg.type is TASK, RESULT, HEARTBEAT, ERROR, or COMMAND.
        if msg.type == MessageType.TASK:
            result = await self._do_work(msg.payload)
            await self.send(msg.reply_to, MessageType.RESULT, result)

    async def on_stop(self):
        # Optional cleanup.
        pass
```

The Supervisor wraps each actor with a `ONE_FOR_ONE` restart policy. If an actor crashes, only that actor is restarted — others keep running. `max_restarts` and `restart_delay` are configurable per actor.

---

## Persistence

Each actor persists state as a `pickle` file at `state/{actor_name}/state.pkl`. The `Actor.persist(key, value)` method writes synchronously to disk on every call. `Actor.recall(key)` reads from the in-memory dict (loaded at startup from the pickle).

> **Cross-agent reads** — Agents can read another agent's pickle directly by navigating to the sibling state directory — useful when two agents need to share large datasets without going through MQTT.

The spawn registry is stored in `state/main/state.pkl` under `_spawned_agents`. On restart, MainActor re-spawns every entry in the registry so dynamic agents and catalog agents survive reboots.

---

## MQTT Topics

| Topic pattern | Publisher | Subscriber | Payload |
|---------------|-----------|------------|---------|
| `io/chat` | IOAgent / UI | main | `{from, content}` |
| `agents/{id}/chat` | any actor | UI / IOAgent | `{role, content, interface}` |
| `agents/{id}/heartbeat` | every actor | MonitorAgent | `{name, state, ts, ...}` |
| `agents/{id}/logs` | any actor | dashboard | `{type, message, ts}` |
| `agents/{id}/manifest` | any actor | main | capabilities, input/output schema |
| `homeassistant/state_changes/#` | HA state bridge | pipeline agents | `{entity_id, domain, new_state, old_state}` |

> **State bridge topic format** — When `HA_STATE_BRIDGE_PER_ENTITY=1` the bridge publishes to `homeassistant/state_changes/{domain}/{entity_id}`. When `=0` (default) it publishes everything to the flat topic `homeassistant/state_changes`. Always subscribe to the wildcard `#` and filter by `entity_id` in the payload.

---

## Message Flow

### User → Agent (any interface)

```
User types:  "@my-agent {"action": "status"}"
  │
  ▼
Interface (CLIInterface / DiscordInterface / RESTInterface / WhatsAppInterface / TelegramInterface)
  │  calls main_actor.process_user_input(text)
  ▼
MainActor._classify_intent()     ← one LLM call: HA | PIPELINE | OTHER
  │
  ├── OTHER  →  main.chat()       ← conversational reply
  ├── HA     →  send to home-assistant-agent
  └── @mention detected  →  send directly to named actor
          │
          ▼
      target-agent.handle_message(msg)
          │
          ▼
      handle_task(agent, payload)  →  returns dict result
          │
          ▼
      main receives RESULT, formats, returns to user
```

### Pipeline (HA state → Discord notification)

```
HA state changes (lamp turns on)
  │
  ▼
HomeAssistantStateBridgeAgent
  │  publishes homeassistant/state_changes  (flat topic, per_entity=0)
  ▼
Mosquitto
  │
  ▼
lamp-on-discord-notify (DynamicAgent)
  │  setup(): agent.subscribe("homeassistant/state_changes/#", on_state)
  │  on_state(): if entity_id == "light.wiz_..." and new_state["state"] == "on":
  │      httpx.post(discord_webhook, {"content": "Lamp is on!"})
  ▼
Discord channel
```

---

## LLM Providers

| Provider | Flag | Env var | Default model |
|----------|------|---------|---------------|
| `AnthropicProvider` | `--llm anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| `OpenAIProvider` | `--llm openai` | `OPENAI_API_KEY` | `gpt-4o` |
| `OllamaProvider` | `--llm ollama --ollama-model llama3` | — | local |
| `NIMProvider` | `--llm nim --nim-model meta/llama-3.3-70b-instruct` | `NIM_API_KEY` | free tier |
| `GeminiProvider` | `--llm gemini --gemini-model gemini-2.5-flash` | `GEMINI_API_KEY` | `gemini-2.5-flash` |

All providers implement `complete(messages, system) → (text, usage)` and `stream(messages, system) → AsyncGenerator`. Cost tracking (USD per 1M tokens) is built into every provider and accumulated in `LLMAgent.metrics`.

---

## Supervision Tree

The supervisor is configured in `cli.py` inside `build_system()`. Each actor is registered with a factory function (called fresh on each restart), a strategy, and restart limits:

```python
system.supervisor
  .supervise("main",     make_main,     strategy=ONE_FOR_ONE, max_restarts=10)
  .supervise("monitor",  make_monitor,  strategy=ONE_FOR_ONE, max_restarts=10)
  .supervise("catalog",  make_catalog,  strategy=ONE_FOR_ONE, max_restarts=10)
  .supervise("home-assistant-agent", make_ha_agent, max_restarts=5)
  # ... etc
```

Dynamic agents (spawned by main or planner) are **not** in the supervision tree — they are managed by the spawn registry. On restart, main re-spawns them from the saved code in `state/main/state.pkl`.

---

## Running Wactorz

The `wactorz` command starts everything — the actor system, an embedded MQTT broker, and the web dashboard. No separate broker process is needed.

```bash
# Start with Anthropic Claude (default interface: CLI)
wactorz

# Choose a different LLM provider
wactorz --llm gemini --gemini-model gemini-2.5-flash
wactorz --llm openai
wactorz --llm ollama --ollama-model llama3
wactorz --llm nim --nim-model meta/llama-3.3-70b-instruct

# Hot-reload in dev (restarts on source file changes)
wactorz --reload

# Discord bot
wactorz --interface discord --discord-token $DISCORD_BOT_TOKEN

# WhatsApp (via Twilio)
wactorz --interface whatsapp

# Telegram
wactorz --interface telegram --telegram-token $TELEGRAM_BOT_TOKEN

# REST API
wactorz --interface rest --port 8080

# Custom MQTT broker (external)
wactorz --mqtt-broker 192.168.1.10 --mqtt-port 1883

# Disable web dashboard
wactorz --no-monitor
```

### Interfaces

| Interface | Flag | Notes |
|-----------|------|-------|
| **CLI** | `--interface cli` | Default. Interactive terminal with streaming responses. |
| **REST** | `--interface rest` | HTTP API on `--port` (default 8080). POST `/chat`, GET `/agents`. |
| **Discord** | `--interface discord` | Bot responds in channels and DMs. Requires `DISCORD_BOT_TOKEN`. |
| **WhatsApp** | `--interface whatsapp` | Via Twilio. Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`. |
| **Telegram** | `--interface telegram` | Bot API. Requires `TELEGRAM_BOT_TOKEN`. |

The web dashboard starts automatically on `http://localhost:8888` regardless of which interface is active, unless `--no-monitor` is passed. It shows live agent status, heartbeats, logs, and a chat interface.