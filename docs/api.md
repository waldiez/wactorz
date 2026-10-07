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

Base URL: `http://localhost:8888/`. Most endpoints accept both `/api/<path>` and `/<path>`; `/api/tts` and `/api/reset` are `/api/`-only.

### `GET /health` · `/healthz` · `/livez`

Liveness probe. Returns `200 OK` with `{"status": "ok"}` whenever the process can
answer. It depends on nothing outside the process, so a broker or Home Assistant
outage never fails it: acting on a liveness failure means restarting, and a restart
would not bring the broker back.

### `GET /ready` · `/readyz`

Readiness probe. Returns `200 OK` once this process should be sent traffic, and
`503 Service Unavailable` until then, naming each check:

```json
{"status": "not ready", "checks": {"supervisor": "ok", "main": "ok", "broker": "disconnected", "database": "ok"}}
```

| Check | Passes when |
|---|---|
| `supervisor` | The supervision tree has started and shutdown has not begun (`not started`, `stopping`) |
| `main` | The `main` agent is running (`missing`, or its state: `failed`, `idle`, `stopped`) |
| `broker` | The connection to the MQTT broker is up (`disconnected`) |
| `database` | SQLite answers a query within 2 seconds (`not open`, `unavailable`) |

A monitor started on its own, with no agents in its process, reports only
`broker`, for the connection it listens on. Agents that depend on Home Assistant
or a device are not checked: the supervisor restarts them, and chat keeps working
while it does.

Every probe path is reachable without a key and under any host name, and is sent
with `Cache-Control: no-store`. The `z` spellings follow the Kubernetes
convention.

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
    "essential":          false,
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

### `GET /api/nodes`

Every remote node main knows: whether it is online, its agents, its latest readings and what its machine is. The readings are the node's last heartbeat; `manifest` is the retained manifest it publishes (`null` from a node that has not sent one). Empty when there is no main.

**Response** `200 OK`
```json
{
  "nodes": [
    {"node": "raspberrypi", "online": true, "agents": ["flic"], "last_seen": 1740000000.0,
     "version": "0.7.0", "runtime": "node", "pid": 1234, "uptime_s": 900.0,
     "cpu_pct": 3.1, "mem_used_mb": 808, "mem_free_mb": 7249, "swap_used_mb": 0,
     "load_1m": 0.1, "load_5m": 0.05, "disk_free_mb": 432492, "temp_c": 56.8, "throttled": [],
     "manifest": {"manifest_v": 1, "arch": "aarch64", "python": "3.13.5", "ram_total_mb": 8058,
                  "devices": ["bluetooth", "speaker", "gpio", "i2c"], "...": "..."}}
  ]
}
```

The fields of `manifest` are described with the `nodes/{node}/manifest` topic in [MQTT topics](mqtt_topics.md).

---

### `GET /api/history/agents/{name}` · `GET /api/history/nodes/{name}`

An agent's or a node's metrics history: one sample about every minute, oldest first, for the last `hours` (default `24`). Kept for `WACTORZ_RETENTION_METRICS_DAYS` (default `7`); an agent is sampled while the dashboard is hearing from it.

**Response** `200 OK`
```json
{
  "agent":          "weather",
  "hours":          24,
  "kept_days":      7,
  "sample_every_s": 60,
  "samples": [
    {"ts": 1740000000.0, "agent": "weather", "node": "", "state": "running",
     "memory_mb": 42.5, "messages_processed": 7, "errors": 0,
     "tasks_completed": 5, "tasks_failed": 0, "cost_usd": 0.0012,
     "queue_wait_p95_s": 0.01, "message_p95_s": 0.2, "task_p95_s": null}
  ]
}
```

A node's samples carry `node`, `online`, `cpu_pct`, `mem_used_mb`, `mem_free_mb`, `agents` (how many it ran), `swap_used_mb`, `load_1m`, `disk_free_mb`, `temp_c` and `throttled` (a list of flags, `[]` for none; `null` where the node could not tell, as for any reading it did not send). `400` when `hours` is not a finite number above 0; `503` when there is no database to keep the history in.

### `GET /api/history/agents`

One field of every agent's samples, in one request: what the dashboard draws each card's trend from. `field` is one of `memory_mb`, `messages_processed`, `errors`, `tasks_completed`, `tasks_failed`, `cost_usd`, `queue_wait_p95_s`, `message_p95_s`, `task_p95_s` (default `messages_processed`); `hours` defaults to `1`.

**Response** `200 OK`
```json
{
  "field": "messages_processed",
  "hours": 1,
  "sample_every_s": 60,
  "agents": {"weather": [[1740000000.0, 7], [1740000060.0, 9]]}
}
```

`400` for a field outside that list or a `hours` that is not a finite number above 0; `503` when there is no database.

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

### `DELETE /api/actors/{actor_id}`

Stop an actor, unregister it, and drop its spawn-registry entry so it is not restored on the next start. **Response** `200 OK` (`stopping ({routed})`), `404` if not found, `403` if the actor is protected.

---

### `POST /api/chat`

Send a chat message to a named agent.

**Request body**
```json
{ "message": "what is the weather?", "agent_name": "main" }
```

`agent_name` is optional and defaults to `main`, the orchestrator.

**Response** `200 OK`
```json
{ "status": "sent", "agent": "main" }
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

### `POST /api/chat/stop`

Cancel every in-flight generation. Takes no request body, and stops all of them
rather than one agent's — there is no per-agent targeting.

```bash
curl -X POST http://localhost:8888/api/chat/stop
```

```json
{ "status": "stopped", "cancelled": 2 }
```

`cancelled` is how many generations were running. Each cancelled stream
finalises and posts `⏹ Stopped.` over the WebSocket.

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
| `GET` | `/health`, `/healthz`, `/livez` | Liveness: `{"status": "ok"}` |
| `GET` | `/ready`, `/readyz` | Readiness: `200` or `503`, same body as the monitor's |
| `GET` | `/metrics` | Prometheus format |
| `GET` | `/ha-map` | HA map snapshot |
| `GET` | `/actors` | List actors |
| `GET` | `/actors/{actor_id}` | Single actor |
| `POST` | `/actors/{actor_id}/message` | `{"content": "..."}` |
| `DELETE` | `/actors/{actor_id}` | Stop, unregister and forget |
| `GET` | `/actors/{actor_id}/metrics` | Metrics |
| `POST` | `/chat` | `{"message": "...", "agent_name": "main"}` |
| `GET` | `/agents` | Alias for `/actors` |
| `POST` | `/agents/command` | `{"target": "name", "command": "start|stop|delete"}` |

#### Chat response shape

```json
{ "status": "sent", "agent": "main", "response": "..." }
```

`agent_name` is optional and defaults to `main`. Any other name reaches that agent
the way typing `@<name> <message>` in chat does: main finds it running, spawns it
from the catalogue, or asks the node it runs on, and `response` is that agent's
reply. A name that is not a single word is refused with `400`.

#### Authentication

Set `API_KEY` in `.env` to require a key on **every** route except the probes. Both
`X-API-Key` and `Authorization: Bearer` are accepted. With no key set the API is
open, which is why the default bind is loopback:

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
    "memory_mb": 69.9
  }
}
```

The dashboard also receives bespoke control frames (`delete_agent`, snapshot diffs, etc.) over the same socket, plus `server_event` frames — the live MQTT activity, relayed by the monitor so the browser never connects to the broker directly.

---

## MQTT

Broker: `mosquitto:1883` (TCP). The dashboard does not use MQTT directly — the monitor relays broker activity to the browser over `/ws` (see above).

All payloads are **snake_case JSON** with `timestamp` as a float (Unix seconds).

See [MQTT Topics](mqtt_topics.md) for the full reference. Key topics:

| Topic | Direction | Notes |
|---|---|---|
| `agents/{id}/heartbeat` | actor → all | Every 10 s. `{actor_id, name, state, memory_mb, task, protected, essential, timestamp, node}`. No CPU figure: agents share one process, whose CPU is on `system/host` |
| `agents/{id}/metrics` | actor → all | Same cadence. LLM agents add `input_tokens`, `output_tokens`, `cost_usd`. |
| `agents/{id}/status` | actor → all | On state change. |
| `agents/{id}/logs` | actor → dashboard | Log entries. |
| `agents/{id}/alert` | monitor / actor | Health / error alerts. Severity: `info|warning|error|critical`. |
| `agents/{id}/commands` | dashboard → actor | `{"command": "start|stop|delete"}`. Protected actors ignore delete; essential ones ignore stop. |
| `agents/{id}/spawned` | parent → all | `{child_id, child_name, timestamp}` when a parent spawns a child. |
| `agents/{id}/manifest` | actor → all | Retained capability manifest. |
| `agents/{id}/chat` | actor → UI | `{role, content, interface, ...}` |
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
| `403` | The command is not allowed for this actor — `delete` on a protected one, `stop` on an essential one |
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
| `ask_agent(agent_name, message)` | `POST /chat` with `agent_name`: main hands it to that agent |
| `list_agents()` | `GET /agents` |
| `list_capabilities(keyword)` | `POST /chat` with `/capabilities` |
| `stop_agent(agent_id)` | `DELETE /actors/{agent_id}` |
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
