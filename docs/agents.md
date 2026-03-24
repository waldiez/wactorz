# Agents

All agents are Python classes implementing the `Actor` protocol. They communicate exclusively via MQTT — no direct function calls between agents.  A Rust implementation mirrors the same actor model and may run in sync as an optional performance layer.

---

## Core (protected) agents

These agents are marked `protected: true` and cannot be stopped or deleted from the dashboard.

### MainActor

|           |                    |
| --------- | ------------------ |
| **Name**  | `main-actor`       |
| **Type**  | `orchestrator`     |
| **Topic** | `agents/{id}/chat` |

The LLM brain of the system. Receives user messages, calls the configured LLM (Anthropic / OpenAI / Ollama), and parses `<spawn>` directives in the response to dynamically create new agents.

**Spawn syntax** (in LLM response):

```xml
<spawn agent-type="dynamic" name="my-agent">
  // Rhai script body
  fn handle(msg) { ... }
</spawn>
```

**Configuration** (`.env`):

```
LLM_PROVIDER=anthropic        # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-6   # any model ID
LLM_API_KEY=sk-ant-...
```

---

### MonitorAgent

|               |                                      |
| ------------- | ------------------------------------ |
| **Name**      | `monitor-agent`                      |
| **Type**      | `monitor`                            |
| **Publishes** | `system/health`, `agents/{id}/alert` |

Polls all registered actors every heartbeat cycle. Raises a `severity: error` alert if any actor's last heartbeat is older than 60 seconds. Publishes a `system/health` digest on every tick.

---

### QAAgent

|             |                                         |
| ----------- | --------------------------------------- |
| **Name**    | `qa-agent`                              |
| **Type**    | `qa`                                    |
| **Listens** | all `*/chat` messages (via MQTT router) |

Passively inspects every chat message flowing through the broker. Flags content that matches harmful patterns (prompt injection, PII, profanity). Publishes a `system/alert` if a policy is violated.

---

## Standard agents

### IOAgent

|             |                                                  |
| ----------- | ------------------------------------------------ |
| **Name**    | `io-agent`                                       |
| **Type**    | `gateway`                                        |
| **Listens** | `io/chat` (fixed topic — no ID discovery needed) |

Bridges the frontend IO bar to the actor system.

- `@agent-name text` → routes `text` to the named agent's mailbox
- No `@` prefix → routes to `main-actor`

---

### NautilusAgent

|          |                  |
| -------- | ---------------- |
| **Name** | `nautilus-agent` |
| **Type** | `transfer`       |

Named after the _nautilus_ shell (SSH = **Secure Shell**) and Jules Verne's submarine. Bridges remote filesystem operations into the chat interface.

**Commands:**

| Command                          | Description             |
| -------------------------------- | ----------------------- |
| `ping <user@host>`               | Test SSH connectivity   |
| `exec <user@host> <cmd [args…]>` | Run a command over SSH  |
| `sync <[user@]host:src> <dst>`   | rsync pull from remote  |
| `push <src> <[user@]host:dst>`   | rsync push to remote    |
| `help`                           | List available commands |

**Examples** (from the IO bar):

```
@nautilus-agent ping deploy@myserver.com
@nautilus-agent exec deploy@myserver.com df -h
@nautilus-agent push ./static/app/ deploy@myserver.com:/opt/wactorz/static/app/
@nautilus-agent exec deploy@myserver.com sudo systemctl restart wactorz
```

**Security**: arguments are never passed through a shell — each token is a discrete `Command::arg()`, preventing injection attacks.

**Configuration** (`.env`):

```
NAUTILUS_SSH_KEY=~/.ssh/wactorz_deploy
NAUTILUS_STRICT_HOST_KEYS=0
NAUTILUS_CONNECT_TIMEOUT=10
NAUTILUS_EXEC_TIMEOUT=120
NAUTILUS_RSYNC_FLAGS=
```

---

### UDXAgent

|          |             |
| -------- | ----------- |
| **Name** | `udx-agent` |
| **Type** | `expert`    |

User and Developer Xpert. Zero-LLM, always-available knowledge agent. Answers questions about Wactorz instantly from a built-in knowledge base — no API key needed.

**Commands:**

| Command             | Description                             |
| ------------------- | --------------------------------------- |
| `help [topic]`      | Overview or topic-specific help         |
| `docs <topic>`      | In-depth documentation                  |
| `explain <concept>` | Explain a concept                       |
| `agents`            | List all live agents (queries registry) |
| `status`            | System health snapshot                  |
| `version`           | Build info                              |

**Topics**: `architecture`, `agents`, `chat`, `dashboard`, `api`, `mqtt`, `deploy`

**Concepts**: `actor-model`, `mqtt`, `hlc-wid`, `rust`, `babylon`, `nautilus`, `io`, `qa`, `monitor`, `dynamic`, `main`, `udx`

**Example** (from the IO bar):

```
@udx-agent help
@udx-agent explain mqtt
@udx-agent docs deployment
@udx-agent status
```

---

### DynamicAgent

|          |                              |
| -------- | ---------------------------- |
| **Name** | `dynamic-{uuid}` (generated) |
| **Type** | `dynamic`                    |

Spawned on-demand by `MainActor` when the LLM response contains a `<spawn>` directive. Executes Rhai scripts generated at runtime. Enables the LLM to extend the system with new capabilities without a server restart.

---

### MlAgent

|          |      |
| -------- | ---- |
| **Type** | `ml` |

Base struct for ML-inference agents. ONNX and Candle backends are currently stubbed (`anyhow::bail!` placeholders) pending full implementation.

---

## Home Assistant agents

These agents integrate with a [Home Assistant](https://www.home-assistant.io/) instance.
They all require at minimum:

```
HA_URL=http://homeassistant:8123
HA_TOKEN=<long-lived access token>
```

### HomeAssistantAgent

|           |                        |
| --------- | ---------------------- |
| **Name**  | `home-assistant-agent` |
| **Type**  | `home-assistant`       |
| **Topic** | `agents/{id}/chat`     |

Unified LLM-backed agent for all Home Assistant operations. Classifies the user's natural-language request with a cheap single-word LLM call, then routes to the appropriate code path.

**Supported intents:**

| Intent               | Description                                                    |
| -------------------- | -------------------------------------------------------------- |
| `recommend_hardware` | Advise which discovered devices/entities can fulfill a request |
| `create_automation`  | Build and persist a new automation via the HA REST API         |
| `edit_automation`    | Update an existing automation                                  |
| `delete_automation`  | Remove an existing automation                                  |
| `list_automations`   | Enumerate all automations                                      |
| `list_areas`         | Enumerate Home Assistant areas                                 |
| `list_devices`       | Enumerate Home Assistant devices                               |
| `list_entities`      | Enumerate Home Assistant entities                              |

Complex operations (`create`, `edit`) use up to two additional LLM calls; simpler ones (`list`, `delete`) use one. If pre-selected `entities` or `hardware` are supplied in the task payload the classification step is skipped and the agent goes straight to automation creation.

**Result schema:**

```json
{
  "result": "human-readable confirmation or list",
  "data": "structured HA API response (list|dict|null)"
}
```

---

### HomeAssistantMapAgent

|               |                                                      |
| ------------- | ---------------------------------------------------- |
| **Name**      | `home-assistant-map-agent`                           |
| **Type**      | `home-assistant-map`                                 |
| **Listens**   | HA WebSocket `entity_registry_updated` events        |
| **Publishes** | `homeassistant/map/entities_with_location` (default) |

Maintains a live map of every HA device with its physical location. Opens a persistent WebSocket connection to Home Assistant and re-fetches the full device/entity/location dataset every time the entity registry changes, then publishes the result downstream.

Output can be directed to an MQTT topic or forwarded directly to another actor by name:

**Configuration (`.env`):**

```
HA_MAP_AGENT_OUTPUT_TOPIC=homeassistant/map/entities_with_location
HA_MAP_AGENT_TARGET_ACTOR=          # optional actor name; overrides MQTT topic
```

**Task commands** (sent via actor mailbox):

| Command          | Description                                                               |
| ---------------- | ------------------------------------------------------------------------- |
| `refresh`        | Force an immediate device-map rebuild and publish                         |
| `refresh simple` | Force an immediate device-map rebuild and publish without entity states   |
| `status`         | Return connection state, event counters, and error info                   |

Large MQTT snapshots are published as multiple messages on the same topic when the
serialized payload is too large. In that case the agent emits a
`home_assistant_map_update_chunked` manifest first, followed by one or more
`home_assistant_map_update_chunk` messages carrying a base64-encoded JSON payload
split into bounded chunks.

**Published payload schema:**

```json
{
  "type": "home_assistant_map_update",
  "event_type": "entity_registry_updated",
  "timestamp": 1234567890.0,
  "event": {},
  "devices": [{ "device_id": "", "name": "", "area": "", "entities": [] }]
}
```

---

### HomeAssistantStateBridgeAgent

|               |                                                                |
| ------------- | -------------------------------------------------------------- |
| **Name**      | `home-assistant-state-bridge`                                  |
| **Type**      | `home-assistant-state-bridge`                                  |
| **Listens**   | HA WebSocket `state_changed` events                            |
| **Publishes** | `homeassistant/state_changes[/{domain}/{entity_id}]` (default) |

Bridges every Home Assistant entity state change to MQTT. Opens a persistent WebSocket connection and forwards each `state_changed` event — optionally filtered by domain and split into per-entity sub-topics.

**Configuration (`.env`):**

```
HA_STATE_BRIDGE_OUTPUT_TOPIC=homeassistant/state_changes   # base MQTT topic
HA_STATE_BRIDGE_DOMAINS=light,switch,sensor                # empty = all domains
HA_STATE_BRIDGE_PER_ENTITY=0                               # 1 = per-entity sub-topics; 0 (default) = single topic
```

When `HA_STATE_BRIDGE_PER_ENTITY=1` each event is published to `{base_topic}/{domain}/{entity_id}`. When `0`, all events share `{base_topic}`.

**Task commands** (sent via actor mailbox):

| Command  | Description                                                            |
| -------- | ---------------------------------------------------------------------- |
| `status` | Return connection state, event counters, domain filter, and error info |

**Published payload schema:**

```json
{
  "type": "home_assistant_state_change",
  "entity_id": "light.living_room",
  "domain": "light",
  "new_state": {},
  "old_state": {},
  "context": {},
  "timestamp": 1234567890.0
}
```

---

### HomeAssistantActuatorAgent

|             |                                                    |
| ----------- | -------------------------------------------------- |
| **Name**    | `actuator-{automation_id}` (truncated to 20 chars) |
| **Type**    | `ha-actuator`                                      |
| **Listens** | One or more configurable MQTT topics               |

Reactive end-of-pipeline actuator. Subscribes to MQTT topics produced by sensor agents (e.g. `DynamicAgent`), evaluates optional conditions against live HA entity states, and calls HA services via a persistent WebSocket connection. One instance per automation.

**Detection pipeline:**

```
MQTT message → detection_filter → cooldown guard → HA conditions → call_service × N
```

Spawned with an `ActuatorConfig` object — not a singleton:

```python
config = ActuatorConfig(
    automation_id="person-light",
    description="Turn on living room light when person detected",
    mqtt_topics=["camera/detections"],
    detection_filter={"label": "person", "confidence": {"gte": 0.7}},
    cooldown_seconds=10.0,
    conditions=[
        ActuatorCondition(
            entity_id="sun.sun",
            attribute="state",
            operator="eq",
            value="below_horizon",
        )
    ],
    actions=[
        ActuatorAction(
            domain="light",
            service="turn_on",
            entity_id="light.living_room",
            service_data={"color_name": "warm_white", "brightness": 200},
        )
    ],
)
```

**`ActuatorConfig` fields:**

| Field              | Type                      | Description                                                                                         |
| ------------------ | ------------------------- | --------------------------------------------------------------------------------------------------- |
| `automation_id`    | `str`                     | Unique identifier for this actuator                                                                 |
| `description`      | `str`                     | Human-readable description                                                                          |
| `mqtt_topics`      | `list[str]`               | Topics to subscribe to                                                                              |
| `actions`          | `list[ActuatorAction]`    | HA service calls to execute on trigger                                                              |
| `conditions`       | `list[ActuatorCondition]` | Optional HA entity conditions (AND logic)                                                           |
| `detection_filter` | `dict \| None`            | Key-value filter on the incoming payload; values may be literals or operator dicts (`{"gte": 0.7}`) |
| `cooldown_seconds` | `float`                   | Minimum seconds between consecutive actuations (default: 10)                                        |

**Condition operators:** `eq`, `ne`, `gt`, `lt`, `gte`, `lte`

**Published actuation record** (`agents/{id}/actuations`):

```json
{
  "automation_id": "person-light",
  "actions": [{ "domain": "light", "service": "turn_on", "entity_id": "..." }],
  "timestamp": 1234567890.0,
  "trigger_payload": {}
}
```

---

## Adding a new agent

1. Create `rust/crates/wactorz-agents/src/my_agent.rs` — implement `Actor` trait (copy `io_agent.rs` as a template)
2. Export in `lib.rs`:
   ```rust
   pub mod my_agent;
   pub use my_agent::MyAgent;
   ```
3. Spawn in `main.rs`:
   ```rust
   let cfg = ActorConfig::new("my-agent");
   let agent = Box::new(MyAgent::new(cfg).with_publisher(publisher.clone()));
   system.spawn_actor(agent).await?;
   ```
4. Add mock responses in `scripts/mock-agents.mjs` for dev-mode testing
5. Add cover gradient + bioline in `frontend/src/ui/SocialDashboard.ts` if desired
