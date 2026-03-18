# Python API Reference

Full interactive docs are auto-generated via [pdoc](https://pdoc.dev) and available at
[`/docs/api/python/`](/docs/api/python/).

---

## Core

### `wactorz.core.actor.Actor`

Abstract base class for all actors in the system. Override `_process(message)` to handle
incoming messages. Actors are identified by a WID-stamped ID and run inside an `ActorSystem`.

**Key methods:**

| Method | Description |
|---|---|
| `_process(message)` | Handle a decoded message dict; must be implemented |
| `start()` | Register and start the actor's receive loop |
| `stop()` | Gracefully shut down the actor |
| `send(to_id, message)` | Send a message to another actor by ID |

---

### `wactorz.core.registry.ActorSystem`

The runtime container that owns the actor registry, message bus, and supervisor tree.
Create one instance per process.

```python
from wactorz.core.registry import ActorSystem

system = ActorSystem()
await system.start()
```

---

### `wactorz.core.registry.Supervisor`

OTP-style supervisor with configurable restart strategies.

| Strategy | Behaviour |
|---|---|
| `one_for_one` | Restart only the crashed actor |
| `one_for_all` | Restart all supervised actors |
| `rest_for_one` | Restart the crashed actor and all defined after it |

```python
from wactorz.core.registry import Supervisor

sup = Supervisor(system, strategy="one_for_one", max_restarts=5, window_s=60)
sup.add("worker", WorkerActor)
await sup.start()
```

---

## Agents

### `wactorz.agents.llm_agent.LLMAgent`

Stateful LLM conversation agent backed by Anthropic Claude. Manages chat history,
streaming responses, tool calls, and MQTT heartbeats.

### `wactorz.agents.main_actor.MainActor`

Orchestrator / router. Receives all inbound WebSocket messages, dispatches to
appropriate agents, and manages session lifecycle.

### `wactorz.agents.monitor_agent.MonitorActor`

Collects CPU/memory metrics, emits MQTT heartbeats on behalf of all running agents,
and forwards alert payloads to the visualization layer.

### `wactorz.agents.io_agent.IOAgent`

Handles file I/O, shell command execution, and code-block tool calls. Runs tasks in a
sandboxed subprocess and streams stdout back through the actor bus.

### `wactorz.agents.code_agent.CodeAgent`

Runs generated code snippets (Python/shell) in an isolated environment, captures
output/errors, and returns results to the calling agent.

---

## Config

### `wactorz.config`

Loads and validates runtime configuration from environment variables and `wactorz.yaml`.

| Symbol | Type | Description |
|---|---|---|
| `Settings` | dataclass | Top-level config container |
| `get_settings()` | `() → Settings` | Returns the singleton config instance |
| `LLMConfig` | dataclass | Model name, API key, temperature, max tokens |
| `MQTTConfig` | dataclass | Broker host/port, topic prefix |

---

> For full method signatures, parameter types, and docstrings see the
> [generated API reference](/docs/api/python/).
