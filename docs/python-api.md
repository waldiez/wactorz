# Python API Reference

Full interactive docs are auto-generated via [pdoc](https://pdoc.dev) and available at
[`/docs/api/python/`](/docs/api/python/).

---

## Core

### `wactorz.core.actor.Actor`

Abstract base class for all actors. Override `handle_message(msg)` to receive messages.
Actors are identified by a deterministic `uuid5` of their name, so the same name always
maps to the same ID across restarts.

**Key methods:**

| Method | Description |
|---|---|
| `async handle_message(msg: Message)` | Handle messages not caught by default handlers; must be implemented |
| `async on_start()` | Called once when the actor starts — launch background tasks here |
| `async on_stop()` | Called on graceful shutdown — cleanup goes here |
| `async start()` | Start the message loop, heartbeat, and command listener |
| `async stop()` | Gracefully shut down the actor and save persistent state |
| `async send(target_id, msg_type, payload)` | Send a typed message to another actor by ID |
| `async broadcast(msg_type, payload)` | Send to all registered actors |
| `async spawn(actor_class, **kwargs)` | Spawn a child actor (inherits MQTT, registry, persistence) |
| `persist(key, value)` | Save a value (auto-routed to SQLite / Redis / Pickle) |
| `recall(key, default=None)` | Load a persisted value |
| `async publish_manifest(description="", publishes=None, capabilities=None, input_schema=None, output_schema=None)` | Publish a retained capability manifest so main can discover this actor |

**Key attributes:**

| Attribute | Type | Description |
|---|---|---|
| `actor_id` | `str` | Deterministic `uuid5` (named) or random `uuid4` |
| `name` | `str` | Human-readable name — also used as persistence folder |
| `state` | `ActorState` | Current lifecycle state |
| `metrics` | `ActorMetrics` | `messages_processed`, `errors`, `uptime`, etc. |
| `protected` | `bool` | If `True`, ignores `stop`, `pause`, and `delete` commands |

**Enums:**

```python
class ActorState(str, Enum):
    IDLE = "idle"; RUNNING = "running"; PAUSED = "paused"
    STOPPED = "stopped"; FAILED = "failed"

class MessageType(str, Enum):
    START = "start"; STOP = "stop"; PAUSE = "pause"; RESUME = "resume"
    DELETE = "delete"; TASK = "task"; RESULT = "result"
    HEARTBEAT = "heartbeat"; SPAWN = "spawn"; TICK = "tick"
    STATUS_REQUEST = "status_request"; STATUS_RESPONSE = "status_response"

class SupervisorStrategy(str, Enum):
    ONE_FOR_ONE = "one_for_one"   # restart only the crashed actor
    ONE_FOR_ALL = "one_for_all"   # restart all siblings
    REST_FOR_ONE = "rest_for_one" # restart crashed actor + all registered after it
```

**Minimal custom actor:**

```python
import asyncio
import time

from wactorz.core.actor import Actor, Message, MessageType

class MyAgent(Actor):

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "my-agent")
        super().__init__(**kwargs)

    async def on_start(self):
        asyncio.create_task(self._poll())

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        result = {"echo": msg.payload}
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    async def _poll(self):
        while True:
            await self._mqtt_publish("custom/my-agent/tick", {"ts": time.time()})
            await asyncio.sleep(10)
```

---

### `wactorz.core.registry.ActorSystem`

The runtime container that owns the actor registry, MQTT publisher, and supervisor.

```python
from wactorz.core.registry import ActorSystem

system = ActorSystem(
    mqtt_broker="localhost",
    mqtt_port=1883,
    state_dir="./state",
)
await system.start()
```

**Key attributes:**

| Attribute | Description |
|---|---|
| `system.registry` | `ActorRegistry` — look up actors, deliver messages |
| `system.supervisor` | `Supervisor` — register factories, start/stop supervision |

---

### `wactorz.core.registry.Supervisor`

Erlang/OTP-style supervision tree. Each actor is registered with an async factory
function (called fresh on each restart), a strategy, and restart limits.

```python
from wactorz.core.actor import SupervisorStrategy

system.supervisor.supervise(
    "my-agent",
    make_my_agent,                             # zero-arg async factory
    strategy=SupervisorStrategy.ONE_FOR_ONE,
    max_restarts=5,
    restart_window=60.0,
    restart_delay=1.0,
)

await system.supervisor.start()
```

To intentionally stop an actor without triggering a restart, call
`supervisor.release(name)` before stopping it — the same pattern used by
`_handle_stop` and `_command_listener` internally.

---

### `wactorz.core.registry.ActorRegistry`

Central message router. Normally you don't instantiate this directly — it is
created by `ActorSystem` and injected into every actor.

| Method | Description |
|---|---|
| `await register(actor)` | Register an actor |
| `await unregister(actor_id)` | Unregister and clean up |
| `await deliver(target_id, msg)` | Deliver a `Message` to an actor's mailbox |
| `await broadcast(sender_id, msg_type, payload)` | Deliver to all registered actors |
| `find_by_name(name)` | Synchronously look up an actor by name |
| `all_actors()` | Returns a `list[Actor]` of all live actors |

---

## Agents

### `wactorz.agents.llm_agent.LLMAgent`

Base class for all LLM-backed agents. Manages conversation history, rolling
summarisation (triggers at 30 messages), and per-call cost tracking.

Providers (all implement `complete(messages, system)` and `stream(messages, system)`):

| Class | `--llm` flag | Primary env var | Universal fallback |
|---|---|---|---|
| `AnthropicProvider` | `anthropic` | `ANTHROPIC_API_KEY` | `LLM_API_KEY` |
| `OpenAIProvider` | `openai` | `OPENAI_API_KEY` | `LLM_API_KEY` |
| `OllamaProvider` | `ollama` | — (no key needed) | — |
| `NIMProvider` | `nim` | `NIM_API_KEY` | `LLM_API_KEY` |
| `GeminiProvider` | `gemini` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `LLM_API_KEY` |

### `wactorz.agents.main_actor.MainActor`

LLM orchestrator. Classifies every user message into `ACTUATE / HA / PIPELINE / OTHER`,
routes to the appropriate agent, and streams replies. Manages the spawn registry
(persisted to SQLite), pipeline rules, user facts, and conversation history.

### `wactorz.agents.dynamic_agent.DynamicAgent`

Executes LLM-generated Python at runtime. Takes a `code` string defining
`setup(agent)`, `process(agent)`, `handle_task(agent, payload)` and runs them in an
async sandboxed loop. See [Agents reference](agents.md#dynamicagent) for the full
`agent` API surface.

### `wactorz.agents.monitor_agent.MonitorActor`

Tracks heartbeat timestamps for every registered actor. Publishes an alert and
notifies main when an actor's last heartbeat is older than `heartbeat_timeout` (default
60 s). Does **not** auto-restart actors — that is the Supervisor's job.

### `wactorz.agents.io_agent.IOAgent`

MQTT ↔ UI gateway. Bridges `io/chat` MQTT messages from the web dashboard to
MainActor and relays responses back to the browser over WebSocket.

### `wactorz.agents.planner_agent.PlannerAgent`

On-demand orchestrator. Spawned per `PIPELINE`-classified request. Discovers
available agents and generates a multi-step plan via LLM. By default MainActor
runs it in `plan_only` mode and stores a pending proposal; after approval, the
planner executes the approved plan, spawns required agents, and self-terminates.

---

## Persistence

### `wactorz.core.persistence`

Three-tier persistence layer routed automatically by key name:

| Store | Location | Used for |
|---|---|---|
| **SQLite** | `state/wactorz.db` | Durable structured data: spawn registry, pipeline rules, user facts, contracts, time-series |
| **Redis** | `redis://localhost:6379` (in-memory fallback if unavailable) | Ephemeral fast-access: observed samples, metrics, heartbeat state |
| **Pickle** | `state/{actor_name}/state.pkl` | Arbitrary Python objects: custom agent state, ML models |

```python
from wactorz.core.persistence import init_persistence, PersistenceAPI

db, redis, pickle_store = init_persistence(
    db_path="./state/wactorz.db",
    redis_url="redis://localhost:6379",
    state_dir="./state",
    run_migration=True,   # migrate existing .pkl files on first run
)

# Attach to an actor
actor._persistence_api = PersistenceAPI(db, redis, pickle_store, actor.name)

# Then from the actor:
actor.persist("my_key", {"value": 42})
actor.recall("my_key")   # → {"value": 42}
```

---

## Config

### `wactorz.config.CONFIG`

Singleton `AppConfig` dataclass loaded from environment variables (`.env` file or
shell environment). Access directly:

```python
from wactorz.config import CONFIG

print(CONFIG.llm_provider)   # e.g. "anthropic"
print(CONFIG.mqtt_host)      # e.g. "localhost"
print(CONFIG.ha_url)         # e.g. "http://homeassistant.local:8123"
```

Key fields mirror the environment variable names in lowercase (e.g. `LLM_API_KEY` →
`CONFIG.llm_api_key`). See [deployment.md](deployment.md#environment-variables) for
the full variable reference.

---

> For full method signatures, parameter types, and docstrings see the
> [generated API reference](/docs/api/python/).
