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
  "cpu":        1.4,
  "memory_mb":  69.9,
  "task":       "idle",
  "protected":  true
}
```

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
  "restart_count":      0
}
```

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
**Trigger:** On state change (start, stop, pause, resume)
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
**Published by:** Dashboard (via `monitor_server.py`) or any external client
**Trigger:** User clicks Pause / Resume / Stop / Delete in dashboard
**Purpose:** Remote control of agents.

```json
{ "command": "pause"  }
{ "command": "resume" }
{ "command": "stop"   }
{ "command": "delete" }
```

> Protected agents (`main`, `monitor`) ignore `pause`, `stop`, and `delete` commands.

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
| `nodes/{node}/migrate` | Main actor | `{ "name": "...", "target_node": "..." }` |
| `nodes/{node}/heartbeat` | Remote runner | `{ "node": "...", "node_id": "...", "agents": [...], "agent_count": 1, "broker": "...", "pid": 123, "uptime_s": 12.3, "cpu_pct": 1.2, "mem_used_mb": 100, "mem_free_mb": 1000 }` |
| `agents/{node}/logs` | Remote runner | `{ "type": "spawned", "message": "...", "node": "...", "timestamp": ... }` |
| `nodes/{node}/logs` | Remote runner | `{ "type": "log", "message": "...", "timestamp": ... }` |
| `nodes/{node}/agents` | Remote runner | `{ "node": "...", "agents": [...] }` |
| `nodes/{node}/migrate_result` | Remote runner | `{ "success": true, "agent": "...", "from_node": "...", "to_node": "..." }` |
| `nodes/{node}/state_return` | Remote runner | `{ "agent": "...", "state": {...}, "return_token": "..." }` |
| `nodes/{node}/reply/#` | Remote runner | Reply payloads for node requests |
| `agents/by-name/{agent}/task` | Main actor | `{ "text": "...", "payload": "...", "_reply_topic": "...", "_remote_task": true }` |

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

# Send a command to an agent (replace {actor_id} with actual UUID)
mosquitto_pub -h localhost -p 1883 -t "agents/{actor_id}/commands" -m '{"command":"pause"}'
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
| `system/health` | Monitor agent | Every 15s |
| `system/host` | Monitor agent | Every 15s |
| `homeassistant/state_changes` | HomeAssistantStateBridgeAgent | HA state change |
| `homeassistant/state_changes/{domain}/{entity_id}` | HomeAssistantStateBridgeAgent | HA state change |
| `homeassistant/state_changes/{entity_id}` | home-assistant-agent | Bootstrap current state |
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
| `agents/by-name/{agent}/task` | Main actor | Remote named-agent task |
