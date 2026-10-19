# Wactorz MQTT Topics Reference

All topics use `localhost:1883` by default (configurable via `--mqtt-broker` and `--mqtt-port`).

---

## Agent Topics

Every agent publishes to its own namespace: `agents/{actor_id}/...`

> **Note:** `{actor_id}` is a deterministic UUID derived from the agent's name.
> Example: `main` → `8070c998-1a59-510e-b64c-bc36b5522a19` (same every restart)

---

### `agents/{id}/heartbeat`
**Published by:** Every agent
**Interval:** Every 10 seconds
**Purpose:** Liveness signal. If absent for 60s, monitor raises an alert.

```json
{
  "actor_id":   "8070c998-...",
  "name":       "main",
  "timestamp":  1740000000.0,
  "state":      "running",
  "memory_mb":  69.9,
  "task":       "idle",
  "protected":  true,
  "essential":  false,
  "node":       ""
}
```

`node` names the machine the agent runs on, and is empty for one running on main.

---

### `agents/{id}/metrics`
**Published by:** Every agent
**Interval:** Every 10 seconds; LLM-backed agents also publish after LLM calls
**Purpose:** Performance counters. LLM agents include cost fields.

```json
{
  "actor_id":           "8070c998-...",
  "messages_processed": 7,
  "errors":             0,
  "uptime":             342.5,
  "tasks_completed":    5,
  "tasks_failed":       0,
  "tasks_timed_out":    0,
  "restart_count":      0,
  "task_p50_s":         0.042,
  "task_p95_s":         0.31
}
```

`tasks_failed` includes the tasks that timed out, which `tasks_timed_out` also counts. Every actor adds `queue_wait_p50_s`/`queue_wait_p95_s` (how long its messages waited in its mailbox) and `message_p50_s`/`message_p95_s` (how long it took over them) once it has handled any. An agent on a node that has asked the model adds `llm_round_trip_p50_s`/`llm_round_trip_p95_s` (each request there through main and back, the model's own time included), `llm_timeouts` (requests main never answered in time) and `llm_unsent` (requests that could not be sent at all, the broker being away). A generated agent also adds `task_p50_s`/`task_p95_s` for its `handle_task`, and `process_p50_s`/`process_p95_s` for its `process()` cycle, over its most recent calls, once it has made any. They are how an agent on a node, which serves no `/metrics`, reports what its work takes.

**LLM agents additionally include:**
```json
{
  "input_tokens":  12480,
  "output_tokens": 3210,
  "cost_usd":      0.085650
}
```

> **Cost is published here** — in `agents/{id}/metrics` alongside token counts.
> LLM agents persist lifetime cost across restarts; short-lived planner agents publish final cost metrics when they stop.

---

### `agents/{id}/status`
**Published by:** Every agent
**Trigger:** On state change (start, stop)
**Purpose:** State transition events.

```json
{
  "actor_id":           "8070c998-...",
  "name":               "main",
  "state":              "running",
  "uptime":             12.3,
  "messages_processed": 0,
  "restart_count":      0,
  "supervised":          false
}
```

---

### `agents/{id}/logs`
**Published by:** MainActor, PlannerAgent, Dynamic agents, and other agents with explicit log publishes
**Trigger:** On user interaction, task completion, spawn events, custom log calls
**Purpose:** Event log entries visible in the dashboard.

```json
{ "type": "user_interaction", "input": "hello", "response": "Hi there!" }
{ "type": "log",     "message": "Camera opened successfully", "timestamp": 1740000000.0 }
{ "type": "spawned", "message": "Spawned 'yolo-agent' on node 'local'", "child_name": "yolo-agent" }
```

---

### `agents/{id}/alert`
**Published by:** Monitor agent, Dynamic agents via `agent.alert()`, and DynamicAgent error handling
**Trigger:** Agent missing heartbeat for > 60 seconds, explicit alert, or structured error
**Purpose:** Health alerts.

```json
{
  "actor_id":  "c0bb7985-...",
  "name":      "code-agent",
  "message":   "[process] RuntimeError('boom')",
  "severity":  "warning",
  "timestamp": 1740000000.0
}
```

Monitor heartbeat alerts use `last_seen_ago` and `state` instead of `message`.

---

### `agents/{id}/commands`
**Published by:** Dashboard (via `wactorz/web/`) or any external client
**Trigger:** User clicks Start / Stop / Delete in dashboard
**Purpose:** Remote control of agents.

```json
{ "command": "start"  }
{ "command": "stop"   }
{ "command": "delete" }
```

> Protected agents (`main`, `monitor`, `catalog`, `installer`) ignore `delete`. `main` is also essential and ignores `stop`.

---

### `agents/{id}/completed`
**Published by:** LLM agents
**Trigger:** After completing a TASK message
**Purpose:** Task completion notification with result preview.

```json
{
  "result_preview": "Here is the bubble sort implementation...",
  "duration": 1.42,
  "task": "write bubble sort"
}
```

---

### `agents/{id}/spawned`
**Published by:** Parent actor when it spawns a child
**Trigger:** On successful spawn
**Purpose:** Spawn notification.

```json
{
  "child_id":   "f6601a20-...",
  "child_name": "yolo-agent",
  "timestamp":  1740000000.0
}
```

---

### `agents/{id}/result`
**Published by:** ML agents, Dynamic agents
**Trigger:** After each continuous loop cycle (if no detections-specific topic)
**Purpose:** Generic inference result.

```json
{
  "result":    "...",
  "timestamp": 1740000000.0
}
```

---

### `agents/{id}/results`
**Published by:** Remote DynamicAgent API
**Trigger:** `agent.publish_result(...)` on a remote node
**Purpose:** Remote generic result wrapper.

```json
{
  "agent": "sensor-agent",
  "node": "rpi-kitchen",
  "result": { "value": 42 },
  "timestamp": 1740000000.0
}
```

---

### `agents/{id}/detections`
**Published by:** Vision agents (YOLO, webcam agents)
**Trigger:** After each frame inference
**Purpose:** Object detection results.

```json
{
  "detections": [
    { "class": "person",  "confidence": 0.923, "bbox": [120.0, 80.0, 400.0, 600.0] },
    { "class": "laptop",  "confidence": 0.871, "bbox": [200.0, 300.0, 500.0, 580.0] }
  ],
  "count":     2,
  "timestamp": 1740000000.0
}
```

---

### `agents/{id}/errors`
**Published by:** Dynamic agents
**Trigger:** Structured setup/process/handler failure
**Purpose:** Error event stream consumed by the monitor.

```json
{
  "actor_id": "c0bb7985-...",
  "name": "camera-agent",
  "phase": "process",
  "error": "camera unavailable",
  "traceback": "...",
  "consecutive": 1,
  "fatal": false,
  "severity": "warning",
  "degraded": false,
  "timestamp": 1740000000.0
}
```

---

### `agents/{id}/manifest`
**Published by:** Discoverable actors and Dynamic agents
**Trigger:** Startup or contract/topic update
**Purpose:** Retained capability and topic-contract metadata.

```json
{
  "name": "temperature-agent",
  "actor_id": "c0bb7985-...",
  "description": "Publishes room temperature",
  "publishes": ["sensors/temperature"],
  "subscribes": [],
  "capabilities": [],
  "input_schema": {},
  "output_schema": {},
  "timestamp": 1740000000.0
}
```

---

### `agents/{id}/actuations`
**Published by:** `HomeAssistantActuatorAgent`
**Trigger:** After actions are executed
**Purpose:** Audit trail for HA actuator pipelines.

```json
{
  "automation_id": "hallway-light",
  "actions": [{ "domain": "light", "service": "turn_on", "entity_id": "light.hallway", "service_data": {} }],
  "timestamp": 1740000000.0,
  "trigger_payload": { "triggered": true }
}
```

---

### `agents/{name}/data/{key}`
**Published by:** Dynamic/remote agent world-state helpers
**Trigger:** `agent.publish_world_state(key, data)`
**Purpose:** Retained agent-scoped shared state.

```json
{ "value": 42, "unit": "C" }
```

---

### `agents/{id}/anomaly`
**Published by:** `AnomalyDetectorAgent`
**Trigger:** When z-score exceeds threshold
**Purpose:** Statistical anomaly events.

```json
{
  "anomaly": true,
  "value":   142.3,
  "zscore":  4.12,
  "mean":    98.5,
  "stdev":   10.6
}
```

---

### `agents/{id}/chat`
**Published by:** Every agent (via `notify_user()`)
**Trigger:** On chat output / assistant reply
**Purpose:** Chat messages. The monitor forwards these to the dashboard chat panel as live output, and the chat channels deliver the ones addressed to them.

```json
{
  "id":        "3f2a9c1e4b7d4e0a9f1b2c3d4e5f6a7b",
  "from":      "main",
  "to":        "user",
  "content":   "Hi there!",
  "timestamp": 1740000000.0
}
```

A message that offers the person a choice, or goes to a chat channel, carries more:

```json
{
  "id":        "3f2a9c1e4b7d4e0a9f1b2c3d4e5f6a7b",
  "from":      "hvac-watch",
  "to":        "user",
  "content":   "Did something change at 14:02?",
  "options":   ["maintenance", "real problem", "not sure"],
  "channel":   "telegram",
  "user":      "123456789",
  "timestamp": 1740000000.0
}
```

`options` are shown as buttons on the dashboard and Telegram, and as a numbered list on Discord and WhatsApp. `channel` names where to deliver it (`dashboard`, `telegram`, `discord`, `whatsapp`; absent means the dashboard alone) and `user` who to reach there: a Telegram or Discord user id, a WhatsApp number. The dashboard shows every frame, whatever channel it names. `id` is what the answer below refers to.

---

### `agents/{id}/answer`
**Published by:** The chat channel the person answered on, or the dashboard server
**Trigger:** The person picks one of a message's options
**Purpose:** The pick, for the agent that asked. The agent subscribes to its own topic to read it; the monitor puts it on the activity feed and tells every open dashboard, which marks the question answered.

```json
{
  "id":        "3f2a9c1e4b7d4e0a9f1b2c3d4e5f6a7b",
  "option":    "maintenance",
  "index":     0,
  "channel":   "telegram",
  "user":      "123456789",
  "from":      "user",
  "timestamp": 1740000600.0
}
```

`id` is the message's; `index` is the option's place among those offered, from zero.

---

### `agents/{id}/spawn`
**Published by:** Discoverable agents
**Trigger:** On startup (self-announce)
**Purpose:** Announce that an agent has come online so listeners can discover it.

```json
{
  "actor_id":  "io-gateway",
  "name":      "io-gateway",
  "timestamp": 1740000000.0
}
```

---

## LLM Bridge & RPC Reply Topics

Remote agents never hold API keys — they route LLM calls through `main`, which
replies on a per-request topic in the asking node's own reply space.

### `main/llm_request`
**Published by:** Agents running on a node (`wactorz-node`)
**Subscribed by:** MainActor's LLM bridge (`agents/main/llm_bridge.py`)
**Purpose:** Centralized LLM calls so no API key leaves `main`.

```json
{
  "messages":     [{"role": "user", "content": "..."}],
  "system":       "...",
  "agent":        "collector",
  "node":         "rpi-kitchen",
  "_reply_topic": "nodes/rpi-kitchen/reply/{id}",
  "_sig":         "{hex HMAC-SHA256}"
}
```

`prompt` may stand in for `messages`. `_sig` is made with the node's signing key
over every other field, written canonically (sorted keys, compact separators);
see `sign_request` in `core/node_signing.py`. Main answers only when
`_reply_topic` is `nodes/<node>/reply/<hex id>` for the `node` the request names,
and refuses an unsigned or wrongly signed request under
`WACTORZ_NODE_SIGNING=enforce` with an error reply.

---

### `main/history_request`
**Published by:** Agents running on a node (`wactorz-node`)
**Subscribed by:** MainActor's LLM bridge (`agents/main/llm_bridge.py`, answered by `agents/main/history.py`)
**Purpose:** `agent.query_ts`, `query_detections`, `query_ha_states` and `ts_stats` on a node, read from main's database.

```json
{
  "query":        "ts",
  "filters":      {"entity_id": "sensor.kitchen", "field": "temp"},
  "hours":        24,
  "since":        1760000000.0,
  "offset":       0,
  "limit":        100000,
  "agent":        "collector",
  "node":         "rpi-kitchen",
  "_reply_topic": "nodes/rpi-kitchen/reply/{id}",
  "_sig":         "{hex HMAC-SHA256}"
}
```

`query` is `ts`, `detections`, `ha_states` or `stats`. Each takes only its own
filters: `topic`, `entity_id` and `field`; `agent`, `class_name` and
`min_confidence`; `entity_id` and `domain`. Signed and admitted exactly as a
`main/llm_request`. `since` and `offset` are absent on the first page; after
that the node sends back the `since` main answered with and the `next` offset,
until `next` is null.

---

### `nodes/{node}/reply/{id}`
**Published by:** MainActor's LLM bridge
**Trigger:** Reply to a `main/llm_request` or a `main/history_request`
**Purpose:** Per-request reply channel for one node (random id, one-shot).

```json
{ "text": "..." }
```

A history read is answered with one page, the rows that fit in one message:

```json
{ "rows": [{"ts": 1760000000.0, "value": 21.5}], "since": 1760000000.0, "next": 812 }
```

`skipped` counts rows too large for any page. A read main refuses is
answered with `{"error": "..."}`, and `stats` with `{"stats": {...}}`.

---

## Shared World State Topics (`home/…`)

Maintained by `SharedStateHub` (`core/topic_bus.py`) as **retained** topics so any
agent can read current state without a request/response round-trip.

### `home/state/{domain}/{entity_id}`
**Published by:** SharedStateHub (`publish_ha_state`)
**Purpose:** Mirror of a Home Assistant entity state (retained).

```json
{ "entity_id": "light.hallway", "state": "on", "attributes": {}, "ts": 1740000000.0 }
```

---

### `home/presence/{zone}`
**Published by:** SharedStateHub (`publish_presence`)
**Purpose:** Occupancy/presence per zone (retained).

```json
{ "zone": "kitchen", "present": true, "people": ["alice"], "source": "", "ts": 1740000000.0 }
```

---

### `home/energy/current`
**Published by:** SharedStateHub (`publish_energy`)
**Purpose:** Current energy consumption (retained).

```json
{ "kwh": 1.2, "cost_per_hour": 0.18, "source": "", "ts": 1740000000.0 }
```

---

## System Topics

### `system/health`
**Published by:** Monitor agent
**Interval:** Every check cycle (15 seconds)
**Purpose:** System-wide health summary.

```json
{
  "timestamp":    1740000000.0,
  "total_actors": 5,
  "running":      4,
  "stopped":      1,
  "failed":       0,
  "degraded":     0,
  "actors": [
    {
      "id":                 "8070c998-...",
      "name":               "main",
      "state":              "running",
      "last_seen_ago":      4.2,
      "consecutive_errors": 0,
      "error_phase":        ""
    }
  ]
}
```

---

### `system/host`
**Published by:** Monitor agent
**Interval:** Every check cycle (15 seconds)
**Purpose:** Host process and memory statistics.

```json
{
  "cpu": 2.5,
  "mem_used_mb": 123.4,
  "mem_total_mb": 16384.0,
  "timestamp": 1740000000.0
}
```

---

### `homeassistant/state_changes` and `homeassistant/state_changes/{domain}/{entity_id}`
**Published by:** `HomeAssistantStateBridgeAgent`
**Trigger:** Home Assistant `state_changed` event
**Purpose:** HA state-change stream for pipelines.

```json
{
  "type": "home_assistant_state_change",
  "entity_id": "light.hallway",
  "domain": "light",
  "new_state": { "state": "on" },
  "old_state": { "state": "off" },
  "context": {},
  "timestamp": 1740000000.0
}
```

`home-assistant-agent` can also bootstrap current entity state to `homeassistant/state_changes/{entity_id}` with `event_type`, `entity_id`, `new_state`, and `old_state`.

---

### `homeassistant/map/entities_with_location`
**Published by:** `HomeAssistantMapAgent`
**Trigger:** On map/location refresh
**Purpose:** Entities that carry location data, for map rendering. Oversized payloads are chunked on the same topic.

```json
{
  "type": "ha_map_entities",
  "entities": [
    { "entity_id": "device_tracker.phone", "lat": 37.98, "lon": 23.72 }
  ],
  "timestamp": 1740000000.0
}
```

---

### `schedule/{name}/fired`
**Published by:** `ScheduledAgent`
**Trigger:** Scheduled fire time or manual trigger
**Purpose:** Time-based pipeline trigger.

```json
{
  "fired_at": "2026-05-28T12:00:00+00:00",
  "schedule_type": "daily",
  "agent": "evening-lights-trigger",
  "manual": false
}
```

---

### `nodes/{node}/spawn`  *(experimental)*
**Published by:** Main actor (for remote node spawning)
**Trigger:** When spawning an agent on a remote node
**Purpose:** Distributed agent deployment.

```json
{
  "name":         "yolo-agent",
  "code":         "async def setup(agent): ...",
  "poll_interval": 0.5
}
```

---

### `models/{name}/active` and `models/{name}/shadow`
**Published by:** Main actor
**Retained:** yes
**Trigger:** At start, for every model kept with `wactorz-artifacts`; then whenever a pointer moves, whether in chat or by the command beside the server, which main notices within a few seconds.
**Purpose:** Where a model's two pointers stand: `active` is the version agents serve, `shadow` one on trial beside it. A spawn that names the model without a version is pinned to `active`. The payload is the pointer as the store keeps it; an unset pointer is an empty retained payload, so a subscriber that comes up late sees the same as one that was there. Read-only for everyone but main: a pointer is moved through the store (`wactorz-artifacts activate|shadow|promote|clear`, `/artifacts` in chat, or `ArtifactStore` from an installed package), never by publishing here.

```json
{
  "name":     "imu-detector",
  "pointer":  "active",
  "version":  3,
  "sha256":   "…",
  "previous": 2,
  "moved_at": 1760000000.0,
  "by":       "cli",
  "reason":   "auc 0.95 on imu-2026"
}
```

---

### Node control and status topics
**Published by:** Main actor and remote runner
**Purpose:** Remote node lifecycle, reconciliation, and task routing.

| Topic | Published by | Payload |
|---|---|---|
| `nodes/{node}/desired_state` | Main actor | `{ "node": "...", "agents": [...], "timestamp": ... }` |
| `nodes/{node}/stop` | Main actor | `{ "name": "...", "delete": true }` |
| `nodes/{node}/stop_all` | Main actor | `{ "reason": "..." }` |
| `nodes/{node}/restart` | Main actor | `{ "reason": "..." }` |
| `nodes/{node}/restart_agent` | Main actor | `{ "name": "..." }` |
| `nodes/{node}/migrate` | Main actor | `{ "name": "...", "target_node": "@main", "return_token": "...", "force": false, "max_state_bytes": 8388608, "max_blob_bytes": 67108864 }` |
| `nodes/{node}/blob/{sha256}` | Main actor | One blob of an agent main is placing on the node, in chunks: each payload is binary, an 8-byte header (chunk index and chunk count, big-endian 32-bit each) followed by up to 256 KiB of the blob. The spawn that follows names the blob in `_initial_state` as `{ "__wactorz_blob__": "<format>", "sha256": "...", "size": ... }`, and the node uses the blob only if its bytes hash to that. Not signed: the spawn is |
| `nodes/{node}/heartbeat` | Remote runner | `{ "node": "...", "version": "0.7.1", "runtime": "node", "node_id": "...", "agents": [...], "agent_count": 1, "broker": "...", "pid": 123, "uptime_s": 12.3, "cpu_pct": 1.2, "mem_used_mb": 100, "mem_free_mb": 1000, "swap_used_mb": 0, "load_1m": 0.4, "load_5m": 0.3, "disk_free_mb": 20000, "temp_c": 56.8, "throttled": [] }` — `version` is the Wactorz release the node runs and `runtime` what kind of process answers (`node` for the package; the single-file runner said `runner`); a node deployed before these fields sends neither and is recorded as `runner` at an unknown version. Memory is in MiB and within any cgroup memory limit on the node (a container, a systemd `MemoryMax=`); `disk_free_mb` is where the node keeps agent state; `temp_c` is the CPU's. `throttled` lists what holds the board back now (`under_voltage`, `freq_capped`, `throttled`, `soft_temp_limit`), from a Raspberry Pi's firmware: `[]` is none, `null` a machine that cannot tell. Any reading the node cannot take is `null`, never 0 |
| `nodes/{node}/manifest` | Remote runner | Retained, at start and after an install: what the machine is. `{ "node": "...", "version": "0.7.1", "runtime": "node", "timestamp": ..., "manifest_v": 1, "arch": "aarch64", "os": "linux", "os_release": "Debian GNU/Linux 13 (trixie)", "python": "3.13.5", "model": "Raspberry Pi 5 Model B Rev 1.0", "container": false, "cpu_count": 4, "ram_total_mb": 8058, "swap_total_mb": 2047, "disk": { "state": { "path": "...", "total_mb": 480001, "free_mb": 432492 }, "venv": { ... } }, "gpu": [], "devices": ["bluetooth", "speaker", "gpio", "i2c"], "packages": { "psutil": "7.1.0", ... } }` — `arch` is the interpreter's, one name per CPU on every system (`aarch64`, `x86_64`, `armv7l` for a 32-bit Pi system); memory and CPUs are within any cgroup limit; `gpu` lists accelerators by `kind` (`cuda`, `hailo`, `edgetpu`, `mps`); `devices` are what an agent can use (`bluetooth` with BlueZ reachable, a `camera` by its driver, `microphone`, `speaker`, `serial`, `gpio`, `i2c`); `packages` are the node environment's distributions by normalised name. `gpu` and `devices` are `null` where the node cannot tell, as is any field it cannot read. Cleared (empty, retained) by `/nodes remove` |
| `agents/{node}/logs` | Remote runner | `{ "type": "spawned", "message": "...", "node": "...", "timestamp": ... }` |
| `nodes/{node}/logs` | Remote runner | `{ "type": "log", "message": "...", "timestamp": ... }` |
| `nodes/{node}/list` | Main actor | *(request)* published to make the runner emit `nodes/{node}/agents` |
| `nodes/{node}/agents` | Remote runner | `{ "node": "...", "agents": [...] }` |
| `nodes/{node}/migrate_result` | Remote runner | `{ "success": true, "agent": "...", "from_node": "...", "to_node": "..." }` |
| `nodes/{node}/state_return` | Remote runner | `{ "agent": "...", "config": {...}, "state": {...}, "state_keys_dropped": [...], "return_token": "..." }`, or with `"refused": "<reason>"` and no state when the agent stays where it is. A blob in `state` is a reference, as on `blob` above. Main places the agent from its own spawn registry entry: `config` is compared with it and never run |
| `nodes/{node}/blob_return/{sha256}` | Remote runner | One blob of an agent the node is handing back, in chunks shaped as on `blob`, sent before the `state_return` that names it. Never a format that runs code when it loads |
| `nodes/{node}/artifact_request` | Remote runner | `{ "agent": "...", "sha256": ["..."] }` — the artifacts an agent spawned here names, pinned in its spawn's `artifacts` as `{ "<key>": { "name": "imu-detector", "version": 3, "sha256": "...", "format": "onnx", "size": ..., "runs_code": false } }`, that the node does not hold. Main answers on `blob` for each that an agent placed on this node names, and the node uses one only if it hashes to what the spawn names |
| `nodes/{node}/reply/#` | Remote runner | Reply payloads for node requests |
| `agents/by-name/{agent}/task` | Main actor | `{ "text": "...", "payload": "...", "_reply_topic": "...", "_remote_task": true }` |

---

## Catalogue Agent Topics

Published by the ready-made agents in `catalogue_agents/`.

| Topic | Published by | Payload |
|---|---|---|
| `custom/sensors/energy/{name}/power` | SmartEnergyAgent | `{ "watts": ..., "timestamp": ... }` |
| `custom/sensors/energy/{name}/cost` | SmartEnergyAgent | `{ "cost": ..., "timestamp": ... }` |
| `sinergym/anomalies/{env_id}` | AnomalyDetectorAgent | anomaly record |
| `wactorz/anomalies/{entity_id}` | AnomalyDetectorAgent | anomaly record |
| `{node}/{name}/detections` | Remote DynamicAgent API | detection payload *(note: not `agents/`-prefixed — a remote-node variant of `agents/{id}/detections`)* |

> **Documented-only namespaces:** `core/topic_bus.py` also describes
> `wactorz/intents/{id}`, `wactorz/results/{id}` (planner intents/results) and the
> generic `custom/{agent}/{stream}` form. These are design conventions — only the
> concrete `custom/sensors/energy/*` topics above are actually published today.

---

## Subscribing with MQTT Explorer / CLI

```bash
# Subscribe to everything from all agents
mosquitto_sub -h localhost -p 1883 -t "agents/#"

# Subscribe to a specific agent's detections
mosquitto_sub -h localhost -p 1883 -t "agents/+/detections"

# Subscribe to all alerts
mosquitto_sub -h localhost -p 1883 -t "agents/+/alert"

# Subscribe to costs/metrics from all agents
mosquitto_sub -h localhost -p 1883 -t "agents/+/metrics"

# System health
mosquitto_sub -h localhost -p 1883 -t "system/#"

# Shared world state (retained)
mosquitto_sub -h localhost -p 1883 -t "home/#"

# Send a command to an agent (replace {actor_id} with actual UUID)
mosquitto_pub -h localhost -p 1883 -t "agents/{actor_id}/commands" -m '{"command":"stop"}'
```

---

## Topic Summary

| Topic | Published by | Interval / Trigger |
|---|---|---|
| `agents/{id}/heartbeat` | Every agent | Every 10s |
| `agents/{id}/metrics` | Every agent | Every 10s; LLM-backed after LLM call |
| `agents/{id}/status` | Every agent | On state change |
| `agents/{id}/logs` | Framework / explicit log publishers | On interaction / log call |
| `agents/{id}/alert` | Monitor / Dynamic agents | On unresponsive detection / explicit alert / error |
| `agents/{id}/commands` | Dashboard / external | On user action |
| `agents/{id}/completed` | LLM agents | After task completion |
| `agents/{id}/spawned` | Parent actor | On child spawn |
| `agents/{id}/result` | ML / Dynamic agents | Per loop cycle |
| `agents/{id}/results` | Remote DynamicAgent API | Remote `publish_result` |
| `agents/{id}/detections` | Vision agents | Per frame |
| `agents/{id}/anomaly` | AnomalyDetectorAgent | On anomaly detected |
| `agents/{id}/errors` | Dynamic agents | On structured error |
| `agents/{id}/manifest` | Discoverable actors | Startup / topic update |
| `agents/{id}/actuations` | HomeAssistantActuatorAgent | After HA actions |
| `agents/{name}/data/{key}` | Dynamic / remote agents | World-state helper |
| `agents/{id}/chat` | Every agent | On chat output |
| `agents/{id}/answer` | A chat channel, or the dashboard server | The person picks one of a message's options |
| `agents/{id}/spawn` | Discoverable agents | On startup (self-announce) |
| `system/health` | Monitor agent | Every 15s |
| `system/host` | Monitor agent | Every 15s |
| `homeassistant/state_changes` | HomeAssistantStateBridgeAgent | HA state change |
| `homeassistant/state_changes/{domain}/{entity_id}` | HomeAssistantStateBridgeAgent | HA state change |
| `homeassistant/state_changes/{entity_id}` | home-assistant-agent | Bootstrap current state |
| `homeassistant/map/entities_with_location` | HomeAssistantMapAgent | Map/location refresh |
| `schedule/{name}/fired` | ScheduledAgent | Schedule fire |
| `nodes/{node}/spawn` | Main actor | On remote spawn |
| `nodes/{node}/desired_state` | Main actor | Remote reconciliation |
| `nodes/{node}/stop` | Main actor | Remote stop/delete |
| `nodes/{node}/stop_all` | Main actor | Remote shutdown/remove |
| `nodes/{node}/restart` | Main actor | Runner restart |
| `nodes/{node}/restart_agent` | Main actor | Remote agent restart |
| `nodes/{node}/migrate` | Main actor | Remote migration |
| `nodes/{node}/heartbeat` | Remote runner | Every 10s |
| `agents/{node}/logs` | Remote runner | Remote spawn/error events |
| `nodes/{node}/logs` | Remote runner | Remote runner events |
| `nodes/{node}/agents` | Remote runner | Agent list response |
| `nodes/{node}/migrate_result` | Remote runner | Migration result |
| `nodes/{node}/state_return` | Remote runner | Remote-to-local state return |
| `nodes/{node}/blob/{sha256}` | Main actor | An agent's blob, in chunks, as main places it, or an artifact the node asked for |
| `nodes/{node}/blob_return/{sha256}` | Remote runner | An agent's blob, in chunks, as the node hands it back |
| `nodes/{node}/artifact_request` | Remote runner | The artifacts an agent spawned here names that the node does not hold |
| `models/{name}/active` | Main actor | Retained: the version of a model agents serve, or empty when none is marked; at start and on each move |
| `models/{name}/shadow` | Main actor | Retained: the version of a model on trial beside the active one, or empty; at start and on each move |
| `agents/by-name/{agent}/task` | Main actor | Remote named-agent task |
| `nodes/{node}/list` | Main actor | Request runner's agent list |
| `main/llm_request` | Remote agents | Route LLM call through main |
| `main/history_request` | Remote agents | Read main's stored history, a page at a time |
| `main/reply/{actor_id}/{uuid}` | Main / RPC responders | Ephemeral reply channel |
| `home/state/{domain}/{entity_id}` | SharedStateHub | HA state mirror (retained) |
| `home/presence/{zone}` | SharedStateHub | Occupancy per zone (retained) |
| `home/energy/current` | SharedStateHub | Energy consumption (retained) |
| `custom/sensors/energy/{name}/power` | SmartEnergyAgent | Per-loop power reading |
| `custom/sensors/energy/{name}/cost` | SmartEnergyAgent | Per-loop cost reading |
| `sinergym/anomalies/{env_id}` | AnomalyDetectorAgent | Anomaly record |
| `wactorz/anomalies/{entity_id}` | AnomalyDetectorAgent | Anomaly record |
| `{node}/{name}/detections` | Remote DynamicAgent API | Remote detections variant |
