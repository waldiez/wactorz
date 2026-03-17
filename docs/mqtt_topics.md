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
**Interval:** Every 10 seconds + immediately after each LLM call  
**Purpose:** Performance counters. LLM agents include cost fields.

```json
{
  "actor_id":           "8070c998-...",
  "messages_processed": 7,
  "errors":             0,
  "uptime":             342.5,
  "tasks_completed":    5,
  "tasks_failed":       0
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
> Costs are accumulated per-agent and reset only when the agent restarts.

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
  "messages_processed": 0
}
```

---

### `agents/{id}/logs`
**Published by:** Every agent  
**Trigger:** On user interaction, task completion, spawn events, custom log calls  
**Purpose:** Event log entries visible in the dashboard.

```json
{ "type": "user_interaction", "input": "hello", "response": "Hi there!" }
{ "type": "log",     "message": "Camera opened successfully", "timestamp": 1740000000.0 }
{ "type": "spawned", "message": "Spawned 'yolo-agent' on node 'local'", "child_name": "yolo-agent" }
```

---

### `agents/{id}/alert`
**Published by:** Monitor agent (on behalf of unresponsive agents)  
**Trigger:** Agent missing heartbeat for > 60 seconds  
**Purpose:** Health alerts.

```json
{
  "actor_id":  "c0bb7985-...",
  "name":      "code-agent",
  "message":   "code-agent unresponsive for 62s",
  "severity":  "warning",
  "timestamp": 1740000000.0
}
```

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
  "result_preview": "Here is the bubble sort implementation..."
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
  "total_agents": 5,
  "running":      4,
  "paused":       0,
  "stopped":      1,
  "failed":       0,
  "alerts":       []
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
| `agents/{id}/metrics` | Every agent | Every 10s + after LLM call |
| `agents/{id}/status` | Every agent | On state change |
| `agents/{id}/logs` | Every agent | On interaction / log call |
| `agents/{id}/alert` | Monitor agent | On unresponsive detection |
| `agents/{id}/commands` | Dashboard / external | On user action |
| `agents/{id}/completed` | LLM agents | After task completion |
| `agents/{id}/spawned` | Parent actor | On child spawn |
| `agents/{id}/result` | ML / Dynamic agents | Per loop cycle |
| `agents/{id}/detections` | Vision agents | Per frame |
| `agents/{id}/anomaly` | AnomalyDetectorAgent | On anomaly detected |
| `system/health` | Monitor agent | Every 15s |
| `nodes/{node}/spawn` | Main actor | On remote spawn |
