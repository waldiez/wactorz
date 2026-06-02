# API Reference

Wactorz exposes four integration surfaces: a REST API, a WebSocket bridge, MQTT pub/sub, and an optional MCP server.

Two REST surfaces are available:

| Server | Port | Started by | Notes |
|---|---|---|---|
| **Monitor server** | `8888` | always-on (unless `--no-monitor`) | Powers the dashboard. Accepts both `/api/*` and bare paths. |
| **`--interface rest`** | `8000` | `wactorz --interface rest` | Generic chat HTTP gateway. Bare paths only (no `/api/` prefix). |

Both publish to the same MQTT broker, so external clients can mix and match.

---

## Monitor REST API (`:8888`)

Base URL: `http://localhost:8888/`. Most endpoints accept both `/api/<path>` and `/<path>`; `/api/tts`, `/api/reset`, `/api/ha/sync`, and `/api/fuseki/*` are `/api/`-only.

### `GET /health`

Liveness probe. Returns `200 OK` with `{"status": "ok"}`.

---

### `GET /api/actors`

List all registered actors with live metrics.

**Response** `200 OK`
```json
[
  {
    "id":                 "8070c998-1a59-510e-b64c-bc36b5522a19",
    "name":               "main",
    "state":              "running",
    "protected":          true,
    "cpu":                1.4,
    "mem":                69.9,
    "task":               "idle",
    "messagesProcessed":  42,
    "costUsd":            0.0156
  }
]
```

---

### `GET /api/actors/{actor_id}`

Get a single actor by ID. Returns the cached MQTT-derived payload from the monitor's state map (richer than the registry view).

**Response** `200 OK` — same shape as a single entry from `/api/actors`, plus metric history.
**Response** `404 Not Found` — actor not in monitor state.

---

### `GET /api/actors/{actor_id}/metrics`

Live metrics for one actor (LLM cost, tokens, messages, errors, restarts).

---

### `GET /api/actors/{actor_id}/history`

Conversation history for the actor (only useful for LLM-backed actors like `main`). Returns the persisted `conversation_history` filtered to `user` + `assistant` roles. Accepts either an `actor_id` (UUID) or a display name (e.g. `main`).

---

### `POST /api/actors/{actor_id}/message`

Send a content message to an actor.

**Request body**
```json
{ "content": "what is the weather?" }
```

**Response** `200 OK` — `{"status": "sent"}`
**Response** `404` if not found, `400` if content missing.

---

### `POST /api/actors/{actor_id}/pause`

Pause a running actor. **Response** `200 OK`, `404` if not found, `403` if protected.

---

### `POST /api/actors/{actor_id}/resume`

Resume a paused actor. **Response** `200 OK`.

---

### `DELETE /api/actors/{actor_id}`

Stop and unregister an actor. **Response** `200 OK` (`stopping ({routed})`), `404` if not found, `403` if protected.

---

### `POST /api/chat`

Send a chat message to a named agent.

**Request body**
```json
{ "message": "what is the weather?", "agent_name": "main-actor" }
```

`agent_name` is optional and defaults to `main-actor`.

**Response** `200 OK`
```json
{ "status": "sent", "agent": "main-actor" }
```

The reply is delivered asynchronously over MQTT (`agents/{id}/chat`) and via the `/ws` WebSocket bridge.

---

### `GET /api/chats`

Query the persistent chat log table. Query parameters:

| Param | Description |
|---|---|
| `agent` | filter by agent name |
| `role` | filter by role (`user` or `assistant`) |
| `since` | Unix timestamp float — only newer rows |
| `limit` | max rows (default 200, max 1000) |

---

### `GET /api/cost`

Return current LLM spend, the active period, and the configured limit.

**Response** `200 OK`
```json
{
  "period":        "monthly",
  "period_key":    "2026-05",
  "spend_usd":     0.0067,
  "limit_usd":     0.70,
  "pct_used":      0.96,
  "limit_reached": false,
  "warning":       false
}
```

`limit_usd` and `pct_used` are `null` when no limit is set.

---

### `POST /api/cost/limit`

Set or update the spend limit at runtime. The override is persisted in SQLite and takes priority over the `LLM_COST_LIMIT_USD` env var.

**Request body**
```json
{ "limit_usd": 0.70, "period": "monthly" }
```

`period` must be `"daily"`, `"weekly"`, or `"monthly"`. Set `limit_usd` to `0` to disable enforcement.

**Response** `200 OK` with `{"ok": true, "limit_usd": 0.70, "period": "monthly"}`.

---

### `POST /api/cost/reset`

Clear accumulated spend for all periods.

**Response** `200 OK` with the reset cost info object.

---

### `POST /api/reset`

Clear stored state and broadcast a reset event over the dashboard WebSocket.

**Request body**
```json
{ "scope": "all", "agent": "optional-agent-name" }
```

`scope` must be one of `"chat"`, `"state"`, `"metrics"`, `"spawns"`, `"logs"`, or `"all"`.
`agent` is optional — when set, the reset is limited to that agent by name.

**Response** `200 OK` with the result of the reset operation; `400` if `scope` is missing or invalid. Also available as the `wactorz-reset` CLI for offline use.

---

### `GET /api/config`

Sanitized runtime configuration (no secrets).

---

### `GET /api/feed`

Recent activity feed events.

---

### `POST /api/ha/sync`

Trigger a Home Assistant snapshot/sync.

---

### Fuseki proxy

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/fuseki/{dataset}/sparql` | SPARQL query proxy |
| `POST` | `/api/fuseki/{dataset}/update` | SPARQL update proxy |

---

### TTS

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/tts/voices` | List available voices |
| `GET` | `/api/tts` | Synthesize speech (`?text=...&voice=...`) |

---

## RESTInterface (`:8000`)

Started with `wactorz --interface rest --port 8000`. Endpoints are at bare paths (no `/api/` prefix).

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/metrics` | Prometheus format |
| `GET` | `/ha-map` | HA map snapshot |
| `GET` | `/actors` | List actors |
| `GET` | `/actors/{actor_id}` | Single actor |
| `POST` | `/actors/{actor_id}/message` | `{"content": "..."}` |
| `POST` | `/actors/{actor_id}/pause` | Pause |
| `POST` | `/actors/{actor_id}/resume` | Resume |
| `DELETE` | `/actors/{actor_id}` | Stop |
| `GET` | `/actors/{actor_id}/metrics` | Metrics |
| `POST` | `/chat` | `{"message": "...", "agent_name": "main"}` |
| `GET` | `/agents` | Alias for `/actors` |
| `POST` | `/agents/command` | `{"target": "name", "command": "stop|pause|resume"}` |

#### Chat response shape

```json
{ "status": "sent", "agent": "main", "response": "..." }
```

#### Authentication

Set `API_KEY` in `.env` to require a key on `/chat`:

```bash
curl -X POST http://localhost:8000/chat \
  -H "X-API-Key: my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "turn off the lights"}'
```

---

## WebSocket Bridge (`/ws`)

Connect: `ws://localhost:8888/ws`

After connection the server streams every MQTT message as a JSON object. Field names match the underlying MQTT payloads (snake_case).

```json
{
  "topic":   "agents/8070c998-1a59-510e-b64c-bc36b5522a19/heartbeat",
  "payload": {
    "actor_id":  "8070c998-1a59-510e-b64c-bc36b5522a19",
    "name":      "main",
    "state":     "running",
    "timestamp": 1709500000.0,
    "cpu":       1.4,
    "memory_mb": 69.9
  }
}
```

The dashboard also receives bespoke control frames (`delete_agent`, snapshot diffs, etc.) over the same socket.

The monitor additionally serves `ws://localhost:8888/mqtt`, a raw passthrough to the broker's WebSocket listener so clients can speak native MQTT over WebSocket through a single port.

---

## MQTT

Broker: `mosquitto:1883` (TCP) / `ws://host:9001` (WebSocket) / `ws://host:8888/mqtt` (proxied via monitor).

All payloads are **snake_case JSON** with `timestamp` as a float (Unix seconds).

See [MQTT Topics](mqtt_topics.md) for the full reference. Key topics:

| Topic | Direction | Notes |
|---|---|---|
| `agents/{id}/heartbeat` | actor → all | Every 10 s. `{actor_id, name, state, cpu, memory_mb, task, protected, timestamp}` |
| `agents/{id}/metrics` | actor → all | Same cadence. LLM agents add `input_tokens`, `output_tokens`, `cost_usd`. |
| `agents/{id}/status` | actor → all | On state change. |
| `agents/{id}/logs` | actor → dashboard | Log entries. |
| `agents/{id}/alert` | monitor / actor | Health / error alerts. Severity: `info|warning|error|critical`. |
| `agents/{id}/commands` | dashboard → actor | `{"command": "stop|pause|resume|delete"}`. Protected actors ignore stop/pause/delete. |
| `agents/{id}/spawned` | parent → all | `{child_id, child_name, timestamp}` when a parent spawns a child. |
| `agents/{id}/manifest` | actor → all | Retained capability manifest. |
| `agents/{id}/chat` | actor → UI | `{role, content, interface, ...}` |
| `io/chat` | UI → IOAgent | `{from, content}` inbound chat from browser. |
| `system/health` | monitor → all | Every 15 s. `{timestamp, total_actors, running, stopped, failed, degraded, actors: [...]}` |
| `homeassistant/state_changes/#` | HA bridge → pipelines | HA state events. |
| `nodes/{name}/spawn` | main → runner | Remote node agent spawn. |
| `nodes/{name}/heartbeat` | runner → all | Remote node liveness. |

---

## Error handling

| HTTP status | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (missing field, invalid period, etc.) |
| `403` | Actor is protected |
| `404` | Actor not found |
| `503` | Registry not available |
| `500` | Internal server error |

MQTT errors are published as `agents/{id}/alert` with a `severity` field.

---

## MCP Server

The optional MCP server lives at `wactorz.interfaces.mcp_server` and is exposed by the `wactorz-mcp` console script when `wactorz[mcp]` is installed. It uses **stdio transport** and calls the Wactorz REST API configured by `WACTORZ_URL`.

```bash
wactorz --interface rest --port 8000
WACTORZ_URL=http://localhost:8000 wactorz-mcp
```

If the script is unavailable in an editable checkout:

```bash
python -m wactorz.interfaces.mcp_server
```

### Environment

| Variable | Description |
|---|---|
| `WACTORZ_URL` | Base URL for the Wactorz REST API. Default: `http://localhost:8000`. |
| `WACTORZ_API_KEY` | Optional API key sent to Wactorz REST as `X-API-Key`. |
| `HA_URL` | Optional Home Assistant base URL for direct HA tools. |
| `HA_TOKEN` | Optional Home Assistant long-lived access token. |

### Tools

| Tool | Backend |
|---|---|
| `ask_wactorz(message)` | `POST /chat` |
| `ask_agent(agent_name, message)` | `POST /chat` with `agent_name` |
| `list_agents()` | `GET /agents` |
| `list_capabilities(keyword)` | `POST /chat` with `/capabilities` |
| `stop_agent(agent_id)` | `DELETE /actors/{agent_id}` |
| `pause_agent(agent_id)` | `POST /actors/{agent_id}/pause` |
| `resume_agent(agent_id)` | `POST /actors/{agent_id}/resume` |
| `ha_list_entities(domain)` | Home Assistant `GET /api/states` |
| `ha_get_state(entity_id)` | Home Assistant `GET /api/states/{entity_id}` |
| `ha_call_service(domain, service, entity_id, data_json)` | Home Assistant `POST /api/services/{domain}/{service}` |

### Resources

| Resource | Backend |
|---|---|
| `wactorz://agents` | `GET /agents` |
| `wactorz://capabilities` | `POST /chat` with `/capabilities` |
| `wactorz://ha-map` | `GET /ha-map` |
| `wactorz://config` | local sanitized config |
