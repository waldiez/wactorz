# API Reference

Wactorz exposes three interfaces: a REST API, a WebSocket bridge, and MQTT pub/sub.

---

## REST API  (`/api/`)

Base URL: `http://host/api/` (proxied by nginx from `:8080`)

### Actors

#### `GET /api/actors`

List all registered actors.

**Response**
```json
[
  {
    "id":        "01JQND5X-a1b2c3d4",
    "name":      "main-actor",
    "state":     "running",
    "protected": true,
    "agentType": "orchestrator"
  }
]
```

---

#### `GET /api/actors/:id`

Get a single actor by ID.

**Response** — same shape as a single entry from `GET /api/actors`

---

#### `POST /api/actors/:id/pause`

Pause a running actor.

**Response** `200 OK` on success, `404` if not found, `403` if protected.

---

#### `POST /api/actors/:id/resume`

Resume a paused actor.

**Response** `200 OK` on success.

---

#### `DELETE /api/actors/:id`

Stop and remove an actor.

**Response** `200 OK` on success, `404` if not found, `403` if protected.

---

### Chat

#### `POST /api/chat`

Send a message to an actor.

**Request body**
```json
{
  "to":      "main-actor",
  "content": "What is the weather in Paris?"
}
```

**Response** `202 Accepted` — message queued.  The reply arrives asynchronously via MQTT `agents/{id}/chat`.

---

### Home Assistant Map

#### `GET /ha-map`

Return the latest cached Home Assistant device map snapshot from `HomeAssistantMapAgent`.

**Response** `200 OK`
```json
{
  "type": "home_assistant_map_update",
  "event_type": "entity_registry_updated",
  "timestamp": 1234567890.0,
  "event": {},
  "devices": []
}
```

**Response** `404 Not Found`
```json
{
  "error": "Home Assistant map snapshot not available"
}
```

---

## WebSocket Bridge  (`/ws`)

Connect: `ws://host/ws`

After connection the server streams every MQTT message as a JSON object:

```json
{
  "topic":   "agents/01JQND5X-a1b2c3d4/heartbeat",
  "payload": {
    "agentId":   "01JQND5X-a1b2c3d4",
    "agentName": "main-actor",
    "state":     "running",
    "timestampMs": 1709500000000
  }
}
```

---

## MQTT

Broker: `mosquitto:1883` (TCP, internal) / `ws://host/mqtt` (WebSocket via nginx)

All payloads are **camelCase JSON**.

### Topic reference

#### `agents/{id}/spawn`

Published by each agent in `on_start()`.

```json
{
  "agentId":     "01JQND5X-a1b2c3d4",
  "agentName":   "main-actor",
  "agentType":   "orchestrator",
  "timestampMs": 1709500000000
}
```

---

#### `agents/{id}/heartbeat`

Published every `heartbeat_interval_secs` (default 10 s).

```json
{
  "agentId":     "01JQND5X-a1b2c3d4",
  "agentName":   "main-actor",
  "state":       "running",
  "timestampMs": 1709500000000
}
```

---

#### `agents/{id}/status`

Published on state changes.

```json
{
  "agentId":     "01JQND5X-a1b2c3d4",
  "state":       "paused",
  "timestampMs": 1709500000000
}
```

---

#### `agents/{id}/alert`

Published by MonitorAgent (stale actor) or QAAgent (policy violation).

```json
{
  "agentId":     "01JQND5X-a1b2c3d4",
  "severity":    "error",
  "message":     "Agent has not sent a heartbeat in 60s",
  "timestampMs": 1709500000000
}
```

`severity` values: `info` | `warning` | `error` | `critical`

---

#### `agents/{id}/chat`

Chat message to or from an agent.

```json
{
  "id":          "WID-abc123",
  "from":        "main-actor",
  "to":          "user",
  "content":     "Here is the weather forecast…",
  "timestampMs": 1709500000000
}
```

---

#### `system/health`

Published by MonitorAgent on every heartbeat tick.

```json
{
  "agentCount":  6,
  "staleAgents": [],
  "timestampMs": 1709500000000
}
```

---

#### `system/spawn`

Published when a DynamicAgent is created (alias for `agents/{id}/spawn` on the `system/` prefix).

---

#### `io/chat`  ← inbound from browser

The fixed topic the frontend IO bar publishes to.

```json
{
  "from":        "user",
  "content":     "@nautilus-agent ping deploy@myserver.com",
  "timestampMs": 1709500000000
}
```

The MQTT event loop routes this to IOAgent's mailbox, which parses the `@mention` and forwards the message body to the target actor.

---

## Error handling

| HTTP status | Meaning |
|---|---|
| `200` | Success |
| `202` | Accepted (async, e.g. chat) |
| `404` | Actor not found |
| `403` | Actor is protected |
| `500` | Internal server error |

MQTT errors are published as `agents/{id}/alert` with `severity: error`.
