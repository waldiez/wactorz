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
     "version": "0.7.2", "runtime": "node", "pid": 1234, "uptime_s": 900.0,
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

`agent_name` is optional and defaults to `main`, the orchestrator. It is a name or a full
address, `name@node` (see [Addressing an agent](agents.md#addressing-an-agent)).

**Response** `200 OK`
```json
{ "status": "sent", "agent": "main" }
```

`404` when no agent has the name; `409` when agents on more than one node have it, with an
`error` that lists their addresses, and when the address names one of them on a node from
before tasks went by node, which would share the task with the others (see
[Addressing an agent](agents.md#addressing-an-agent)).

The reply is delivered asynchronously over MQTT (`agents/{id}/chat`) and via the `/ws` WebSocket bridge.

---

### `GET /api/chats`

Query the persistent chat log table. Query parameters:

| Param | Description |
|---|---|
| `agent_id` | one agent's history, by its id |
| `agent` | filter by agent name: every agent that went by it |
| `role` | filter by role (`user` or `assistant`) |
| `since` | Unix timestamp float — only newer rows |
| `limit` | max rows (default 200, max 1000) |

An agent's question is stored as its words with the options numbered under them. A row that
is a question the ledger still knows (`GET /api/questions`) carries it, so the dashboard can
show it with its buttons, or with the pick once it is answered:

```json
{"id": 41, "ts": 1760000000.4, "agent_name": "hvac-watch", "agent_id": "…", "role": "assistant",
 "content": "Did something change at 14:02?\n\n1. maintenance\n2. real problem",
 "question": {"id": "3f2a…", "agent_id": "…", "text": "Did something change at 14:02?",
              "options": ["maintenance", "real problem"], "answer": null}}
```

The same words asked more than once are told apart by time: a row is the question asked
nearest it.

A row's `agent_id` is the id of the agent whose thread the turn is in, and the history is kept
by it: two agents may share a name, and `agent` returns the rows of both. A row stored before
ids were kept has the id its agent had then, derived from its name.

---

### `GET /api/commands`

The slash commands the chat can send, each way to type them, for the dashboard's chat box to offer as a person types `/`. They come from the orchestrator that answers the chat (main's commands, the model-free profile's few, or an orchestrator's own by name), and from the server for the few it answers itself before any orchestrator: `/agents` where nothing else does, `/deploy`, `/migrate` and `/clear-plans`.

**Response** `200 OK`
```json
{
  "commands": [
    {"usage": "/plans approve <id>", "typed": "/plans approve", "summary": "execute a proposed pipeline"}
  ]
}
```

`typed` is what goes in the box before the first argument. `/help` lists the same forms.

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
`agent` is optional — when set, the reset is limited to that agent, by name or `name@node`, for `chat`, `state`, `metrics` and `spawns`. `logs` is this server's log files, which every agent shares, and `all` is a factory reset, which stops and wipes everything whatever agent is named; both ignore it. A `chat` reset for one agent deletes the uploaded files only that agent's thread referred to; a file another thread also shows is kept. A `state` reset also restarts each agent it covers on a node, and each on main that the supervisor runs, so the agent forgets what it read when it started as well as the file; the system's own agents (main, the monitor, the installer, the catalogue), and any the supervisor does not hold, keep running with their copies cleared.

`chat`, `state` and `metrics` also reach the agents on nodes: main asks each node concerned to clear its own copies, and waits up to 10 s for it to say so.

**Response** `200 OK` with `{"status": "ok", "scope": ..., "agent": ..., "nodes": {...}}`. `nodes` (empty for `all`, which deletes the agents on nodes rather than resetting them) says, for each node the reset concerned, `"cleared"`, `"no answer"`, `"offline"` (not asked: the request is not kept for it), or `"older release"` (a node that does not reset its agents; deploy it again). `400` if `scope` is missing or invalid; `404` if `agent` is an address and the name is not on that node; `409` if `agent` is a bare name in more than one place, main and nodes alike (the `error` lists the addresses), or an address whose agent's id cannot be found. An address of a name in more than one place resets that agent alone, and the others of the name keep everything. Also available as the `wactorz-reset` CLI for offline use.

---

### `GET /api/config`

Sanitized runtime configuration (no secrets).

---

### `GET /api/feed`

Recent activity feed events.

---

### `GET /api/models`

Every model kept with `wactorz-artifacts`: its newest version and where its pointers stand.

**Response** `200 OK`
```json
{
  "models": [
    {
      "name": "imu-detector",
      "newest": {"name": "imu-detector", "version": 4, "format": "npz", "size": 2048,
                 "sha256": "…", "metadata": {"caught": 0.92}, "reference": "imu-detector:4", "…": "…"},
      "versions": 4,
      "pointers": {
        "active": {"name": "imu-detector", "pointer": "active", "version": 3, "previous": 2,
                   "by": "dashboard", "reason": "auc 0.95", "moved_at": 1760000000.0,
                   "sha256": "…", "holds": true}
      }
    }
  ]
}
```

A pointer's `holds` is false when the version it names no longer holds the bytes it was set to, which happens once a removed version's number is reused: move the pointer.

---

### `GET /api/models/{name}`

One model: every version, oldest first, with what was said of it when it was stored (`metadata`), and its pointers as above. `404` when no version of `name` is stored, `400` for a name that cannot be one (letters, digits, `.`, `_` and `-`, starting with a letter or digit).

---

### `GET /api/models/{name}/moves`

Every move of the model's pointers, newest first: who made it, why, and from which version to which. The store journals each change as it is made, whoever made it (the dashboard, the chat, the CLI beside the server), and main records the journal in the provenance graph on its next look. `limit` caps how many (default 200, at most 1000).

**Response** `200 OK`
```json
{"name": "imu-detector",
 "moves": [
   {"at": 1760000300.0, "pointer": "shadow", "change": "promoted", "version": null, "from": 4,
    "by": "dashboard", "reason": "won the trial"},
   {"at": 1760000300.0, "pointer": "active", "change": "moved", "version": 4, "from": 3,
    "by": "dashboard", "reason": "won the trial"}
 ]}
```

`change` is `moved` (set to `version`), `cleared` (unset, with the clear's own `reason`; `by` is empty for a pointer dropped by removing every version of the model), or `promoted` (the shadow cleared by a promotion, with the promotion's `by` and `reason`). Every change is kept, however quickly the next follows; one made while main was down is recorded when it starts. The history outlives the versions, so a model whose versions were all removed still has its moves. `400` for a bad `name` or `limit`, `503` when persistence is not running.

---

### `POST /api/models/{name}/active` · `POST /api/models/{name}/shadow`

Point `active`, the version agents serve, or `shadow`, the one on trial beside it, at a version.

**Request body**
```json
{"version": 3, "reason": "auc 0.95 on imu-2026"}
```

`reason` is optional. The move is recorded as made `by` `dashboard`, as the CLI's are `by` `cli`. Pointing `active` at the shadow version promotes it.

**Response** `200 OK`: `{"pointer": {...}}`, where the pointer stands now. Main publishes it on `models/{name}/{pointer}` within a few seconds.

---

### `POST /api/models/{name}/promote`

Make the shadow version active and end the trial. Body optional: `{"reason": "..."}`.

---

### `POST /api/models/{name}/rollback`

Move `active` back to the version it named before its last move. Only that one step is kept, so a second rollback returns to where the first started. Body optional: `{"reason": "..."}`.

---

### `DELETE /api/models/{name}/shadow`

End the trial: unset `shadow`. The body is optional: `{"reason": "..."}` is kept with the clear in the history.

---

### `GET /api/models/{name}/trial`

What the shadow trial shows: the pairs the serving agents kept, compared. By default the trial's own window, from when `shadow` was set to now; `since` and `until` (Unix seconds) choose another, and are the only way to read a trial that has ended. `source` and `positive` (label values, comma separated) add each side's precision and recall over the pairs a label of that source covers, as `GET /api/labels/effective` resolves them.

**Response** `200 OK`
```json
{"name": "imu-detector", "shadow": {"version": 4, "moved_at": 1760000000.0, "...": "..."},
 "since": 1760000000.0, "until": 1760086400.0, "source": "sensors/imu/left-wrist",
 "pairs": 8640, "agreement": 0.97, "flagged": {"active": 41, "shadow": 38}, "shadow_errors": 0,
 "versions": {"active": [3], "shadow": [4]},
 "latency": {"active": {"mean_ms": 1.2, "p95_ms": 2.9}, "shadow": {"mean_ms": 1.4, "p95_ms": 3.1}},
 "score": {"active_mean": 2.1, "shadow_mean": 2.0, "difference_mean": -0.1, "scored": 8640},
 "labels": {"labelled": 120, "positives": 9, "positive": ["fault"],
            "active": {"tp": 7, "fp": 3, "fn": 2, "precision": 0.7, "recall": 0.78},
            "shadow": {"tp": 8, "fp": 1, "fn": 1, "precision": 0.89, "recall": 0.89}}}
```

`agreement` is the share of pairs where both sides answered alike; `flagged` how many inputs each side published something for; `score` is read from a `score` field in the outputs, over the pairs where both gave one. `404` when no shadow is set and no window is given, `400` for a bad `name`, `since` or `until`, `503` when the pairs cannot be read because the `data` extra is not installed, or when labels are asked for and persistence is not running.

---

### `GET /api/models/{name}/trial/series`

The same pairs over time, for a chart: the window as above, cut into `points` stretches of equal length (120 unless said, at most 1000).

**Response** `200 OK`
```json
{"name": "imu-detector", "since": 1760000000.0, "until": 1760086400.0,
 "buckets": [{"ts": 1760000360.0, "pairs": 72, "active_score": 2.1, "shadow_score": 2.0,
              "active_ms": 1.2, "shadow_ms": 1.4},
             {"ts": 1760001080.0, "pairs": 0, "active_score": null, "shadow_score": null,
              "active_ms": null, "shadow_ms": null}]}
```

Every stretch is there, with `pairs` 0 and nulls where no input came, so a chart can show the gap. `ts` is the middle of the stretch; each value is that side's mean over it. `400` for a bad `points`, and otherwise the same answers as `.../trial`.

---

### `GET /api/models/{name}/trials`

The outcomes of the model's trials that ended, newest first: how each ended (`promoted` or `cleared`), the versions on either side, the window, and the comparison above as it stood then. `503` when persistence is not running.

---

Every pointer move answers `200 OK` with `{"pointer": {...}}`, the pointer it moved as it stands now; for `DELETE .../shadow`, the shadow pointer as it was before it was cleared. Each answers `409` for a move the pointers do not allow now (already there, no shadow to promote, nothing to roll back to, a shadow on the active version), `404` for a name or version that is not stored, and `400` for a request that is not one; `{"error": "..."}` says which in the store's words.

---

### `GET /api/questions`

The questions agents have put to a person, on any channel, from main or a node: the open ones oldest first, and the newest answered ones.

**Response** `200 OK`
```json
{
  "open": [
    {"id": "3f2a…", "agent_id": "…", "agent": "hvac-watch",
     "text": "Did something change at 14:02?", "options": ["maintenance", "real problem"],
     "channel": "telegram", "user": "123456789", "asked_at": 1760000000.0,
     "answer": null, "answer_index": null, "answer_channel": "", "answered_at": 0.0,
     "answer_note": "", "fallen_back": false, "label": null,
     "context": "Supply air rose 6 degrees in ten minutes."}
  ],
  "answered": []
}
```

Read from the books agents publish on `agents/{id}/questions`. `label` is the label spec a question was asked with (`ask_operator(..., label=...)`), or `null`; see `GET /api/labels`. `context` is why the agent says it asks (`ask_operator(..., context=...)`), or empty; the dashboard's Inbox shows it, and chat channels are sent the question alone.

---

### `POST /api/questions/{agent_id}/{question_id}/answer`

Pick one of a question's options, as the dashboard's button does. The first pick on any channel is the answer.

**Request body**
```json
{"option": "maintenance", "note": "valve replaced at 14:02"}
```

`note` is optional: what the person adds in their own words, up to 500 characters. The agent keeps it with the answer as `answer_note`.

**Response** `200 OK`: `{"answer": {"id": "...", "option": "maintenance", "index": 0, "channel": "dashboard", ...}}`, the pick as it went to the agent on `agents/{agent_id}/answer`. `404` for a question that is not in `GET /api/questions`, `409` for one already answered, `400` for an option it did not offer, `503` when the broker is not connected.

A `200` says the pick was sent, not that it was first: the `409` is checked against the ledger, which learns of an answer only when the agent publishes its book again. Two picks sent at nearly the same moment can both be sent, and the agent keeps the one that reaches it first. `GET /api/questions` shows which.

The dashboard's button is checked the same way when the question is listed. A message sent with `notify_user` alone keeps no book, so its pick goes to the agent unchecked, for the agent to judge.

The installer's question about a package and main's about running model-written code are listed like any other, with `"permission": true`. They are settled only by a pick made here or on the dashboard, in the server's own process: one published on `agents/{agent_id}/answer` changes nothing. They close with `answer` set to what ended them when no pick came: `No answer in time`, `Allowed` or `The server restarted first`.

---

### `GET /api/labels`

Labels: what a source was doing over a period, as the operator, an agent or a rule said. Oldest recorded first. Query parameters, all optional:

| Parameter | Meaning |
| --- | --- |
| `source` | Only labels on this topic or entity |
| `since`, `until` | Only labels whose window touches this stretch of event time, in Unix seconds |
| `all` | `1` to include labels a later one has superseded entirely |
| `limit` | At most this many, up to 5000 (default 1000) |

**Response** `200 OK`
```json
{
  "labels": [
    {"id": "…", "source": "sensors/ahu-1", "start": 1760000000.0, "end": null,
     "value": "maintenance", "status": "partly_superseded", "recorded": 1760001860.0,
     "actor": "operator", "node": "rpi", "channel": "telegram", "note": "valve replaced",
     "question_id": "3f2a…", "model": "", "verified": false}
  ]
}
```

A label is written, never here, by an answer to a question asked with a label spec, by an agent's `agent.label(...)` on main, or by a rule's `label` action. A later label on the same source supersedes an earlier one where their windows overlap: the earlier one stays, `partly_superseded` or `superseded`. `verified` is `true` for a label from an answer the dashboard sent, from an agent or from a rule, and `false` for one from an answer heard on the broker, which anything there could have published. `400` for a parameter that is not what it should be, `503` when persistence is not running. See `docs/design/labels-and-provenance.md`.

---

### `GET /api/labels/effective`

What one `source` was doing, moment by moment: the newest label at each moment over `since` to `until` (Unix seconds, both optional). Adjacent stretches of one label are one interval, and a stretch no label covers is left out.

**Response** `200 OK`
```json
{
  "source": "sensors/ahu-1", "since": null, "until": null,
  "intervals": [
    {"start": 1760000000.0, "end": 1760003600.0, "value": "maintenance",
     "label_id": "…", "actor": "operator", "channel": "telegram", "verified": false},
    {"start": 1760003600.0, "end": null, "value": "fault",
     "label_id": "…", "actor": "operator", "channel": "dashboard", "verified": true}
  ]
}
```

`400` without a `source`, `503` when persistence is not running.

---

### `GET /api/logs`

Recent application log lines, oldest first, from an in-memory buffer of the last thousand or so; the same lines the dashboard's log view shows. Redacted on the way in.

Query, all optional: `limit` (default `200`, at most `500`), `level` (this level and above), `logger` (part of the logger's name), `turn` and `agent`.

A line written while a person's message was being answered names its `turn`: the id given to that message where it entered, which travels with everything done to answer it, through main, the planner and the agents it asks, their model and Home Assistant calls, and a task sent to an agent on a node. `?turn=` returns the lines of one answer. A line also names the `agent` that was at work when it was written, whatever logger wrote it; `?agent=` matches the name whole.

**Response** `200 OK`
```json
{
  "entries": [
    {"source": "app", "ts": 1740000000.0, "level": "INFO", "origin": "wactorz.agents.main.actor",
     "text": "[main] Intent: PIPELINE — …", "turn": "eea5bad62007", "agent": "main"}
  ],
  "capacity": 1000
}
```

An agent's own errors and log events carry the turn as well (`turn`, on `agents/{id}/errors` and `agents/{id}/logs`).

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

### Chat frames

A chat turn is sent as `{"type": "chat", "content": "...", "agent_name": "main", "agent_id": "…", "id": "…"}`: the thread the page is in, by name and id, and the page's id for the message. Only `content` is required.

The server answers first with the agent the turn reached, which is the agent an `@mention` names when it can reach one, and otherwise the thread the page said:

```json
{"type": "turn", "agent_id": "…", "name": "catalog", "id": "…", "timestamp": 1760000000.4}
```

`id` is the message's own, so the page files the person's words in that agent's thread. Every reply frame that follows (`chat`, `stream_chunk`, `stream_end`) carries the same `from` (the agent's name) and `agent_id`.

A relayed `server_event` for an agent's chat message carries `thread_id`, the id of the agent whose thread it is in, when that is not the agent whose topic it came on: a voice surface relays the person's words to the agent that answers them, which it names only by name.

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
