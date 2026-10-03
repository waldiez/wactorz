# Prometheus Monitoring

This deliverable adds Prometheus-based monitoring for the **Python** Wactorz runtime.

## Scope

Included:

- Python REST API metrics at `/metrics`
- actor health and runtime metrics from the Python registry
- process/runtime metrics from the Python process
- Prometheus in Docker Compose, and Alertmanager to deliver its alerts
- optional Mosquitto availability probe controlled by `.env`

## What Is Monitored

### Python app

Prometheus scrapes the Python REST service and records:

- HTTP request counts
- HTTP response counts by status
- HTTP request duration
- running actor count
- actor state
- actor heartbeat age
- actor restart count
- actor messages processed
- actor errors
- actor tasks completed and failed
- LLM input tokens
- LLM output tokens
- LLM cost in USD
- process/runtime metrics exported by `prometheus_client`

And, for what the dashboard does not show:

| Metric | What it says |
|---|---|
| `wactorz_mqtt_connected` | `1` while the server's broker connection is up |
| `wactorz_mqtt_outbox_queued` | Messages in memory waiting to be sent to the broker |
| `wactorz_mqtt_outbox_backlog` | Stored messages waiting on disk for room in that queue |
| `wactorz_mqtt_publish_failures_total` | Publishes that failed on a live connection and were held to retry |
| `wactorz_mqtt_outbox_dropped_total` | Messages discarded because the outbox was full |
| `wactorz_mqtt_outbox_discarded_total` | Messages given up on: unsendable, expired undelivered, or failing every try |
| `wactorz_actor_mailbox_depth{actor_name}` | Messages waiting in an actor's mailbox |
| `wactorz_actor_messages_refused_total{actor_name}` | Messages a full mailbox had no room for: notifications dropped, anything else refused after a wait |
| `wactorz_actor_handling_seconds{actor_name}` | How long an actor has been on the message it is handling; `0` when idle. An actor's heartbeat carries on while it waits on one message, so this is what shows it stuck |
| `wactorz_event_loop_lag_seconds` | Histogram of how long the event loop took to run a callback it was asked to run at once. Every agent shares the loop, so a long lag is all of them waiting |
| `wactorz_nodes{state}` | Edge nodes that are `up` and `down` |
| `wactorz_node_up{node}` | `1` while a node's heartbeat is recent |
| `wactorz_node_heartbeat_age_seconds{node}` | Seconds since a node's last heartbeat |
| `wactorz_node_agents{node}` | Agents a node reported running |
| `wactorz_node_info{node,version,runtime}` | The version and runtime a node reported |
| `wactorz_llm_requests_total{provider,outcome}` | LLM requests by how they ended: `ok`, `unavailable` (the provider kept failing through every retry) or `error` (anything else: a rejected request, a bad key) |
| `wactorz_llm_request_duration_seconds{provider}` | Time from a request to its answer or failure, retries included; for a streamed answer, to its last chunk |

A request counts once however many attempts it took, and one the caller cancelled is not counted. Main forgets a node that stays silent, so the nodes named in your deploy targets are reported as down until they are heard from, rather than disappearing; a node started by hand shows only while main knows it.

The app exposes these at:

```text
GET /metrics
```

### Mosquitto

Mosquitto is an **optional** Prometheus target.

It is monitored with the Blackbox Exporter:

- Mosquitto: TCP connect probe to `mosquitto:1883`

This is availability monitoring, not deep service-specific exporter telemetry.


## Authentication

`/metrics` is served by the API on port 8000, and once `API_KEY` is set every
route there except the health probes requires it. An unauthenticated scrape gets `401`
and the target goes down with nothing written to the log, so a keyed install
loses its metrics silently unless the scrape carries the key.

The endpoint accepts either `X-API-Key` or `Authorization: Bearer`.

**Bundled Prometheus** needs no setup: the compose stack passes `API_KEY`
through and the scrape config is rendered with the header when a key is set.

**Your own Prometheus** must be told:

```yaml
  - job_name: wactorz-python
    static_configs:
      - targets: ["wactorz:8000"]
    authorization:
      type: Bearer
      credentials: 'your-api-key'
```

Prefer `credentials_file` where the scraper reads its configuration from a
shared location — Prometheus redacts `credentials` from its own config API, but
a file keeps the key out of the config entirely.

⚠ The key is the only credential there is, so a scraper holding it can reach
every API route, not only `/metrics`. That is reasonable where Prometheus runs
beside the app and inside the same trust boundary; it is worth more thought
across a network.

## Environment Flags

Add or adjust these in `.env`:

```env
PROMETHEUS_EXTERNAL_PORT=9090
PROMETHEUS_SCRAPE_INTERVAL=15s
PROMETHEUS_PYTHON_TARGET=wactorz-python
PROMETHEUS_MONITOR_MOSQUITTO=0
REST_EXTERNAL_PORT=8000
```

Notes:

- `PROMETHEUS_PYTHON_TARGET` chooses the host or service name Prometheus scrapes for Python metrics; `REST_EXTERNAL_PORT` is appended as the scrape port.
- If Wactorz runs in Compose, use the service name such as `wactorz-python` or `wactorz`.
- If Wactorz runs from the terminal on the host, use `host.docker.internal`.
- `PROMETHEUS_MONITOR_MOSQUITTO=1` enables the Mosquitto TCP probe.

## Docker Compose

### Main stack

Prometheus, Alertmanager and the Blackbox Exporter belong to the `python` profile:

```bash
docker compose --profile python up -d
```

The `full` profile adds Home Assistant and does not start them; give both profiles to have both:

```bash
docker compose --profile python --profile full up -d
```

### Development stack

There they belong to the `app` profile:

```bash
docker compose -f compose.dev.yaml --profile app up -d
```

Prometheus is available at:

```text
http://localhost:${PROMETHEUS_EXTERNAL_PORT:-9090}
```

## Simple Ways To Run It

### 1. Wactorz in Compose, Prometheus in Compose

Leave:

```env
PROMETHEUS_PYTHON_TARGET=wactorz-python
```

Then run:

```bash
docker compose --profile python up -d prometheus alertmanager blackbox-exporter wactorz-python
```

### 2. Wactorz from terminal, Prometheus in Compose

Set:

```env
PROMETHEUS_PYTHON_TARGET=host.docker.internal
```

Start Wactorz locally in REST mode, then run:

```bash
docker compose --profile python up -d --no-deps prometheus alertmanager blackbox-exporter
```

This starts only the monitoring containers and points Prometheus at the Wactorz process running on your host.

## Verification

### 1. Check Python metrics directly

```bash
curl -fsS http://localhost:8000/metrics | head

# With API_KEY set:
curl -fsS -H "Authorization: Bearer $API_KEY" http://localhost:8000/metrics | head
```

You should see Prometheus-formatted output such as `wactorz_actors_total`, `wactorz_http_requests_total`, and process metrics.

### 2. Check Prometheus targets

Open:

```text
http://localhost:9090/targets
```

Expected:

- `prometheus` is `UP`
- `wactorz-python` is `UP`
- optional probe targets appear only when enabled in `.env`

### 3. Check optional probes

When `PROMETHEUS_MONITOR_MOSQUITTO=1`, Prometheus should show a `mosquitto-blackbox` target.

If a flagged dependency is not running, that target will correctly show as failing.

## Alert Rules

Basic Prometheus alert rules are included for:

- Python app down
- actor heartbeat stale
- an actor on one message for over 30 minutes
- the event loop blocked for more than 5 seconds
- the broker connection lost for 2 minutes
- more than 100 outgoing messages waiting for 10 minutes
- outgoing messages dropped or given up on
- an edge node down for 5 minutes
- more than half the requests to an LLM provider failing for 10 minutes
- optional dependency probe failing

They live in `infra/prometheus/alerts.yml`. Prometheus evaluates them and hands the ones that fire to Alertmanager, which the compose stack starts beside it.

## Where Alerts Go

Nowhere, until you say. Out of the box Alertmanager lists what is firing on its own page and delivers nothing:

```text
http://localhost:${ALERTMANAGER_EXTERNAL_PORT:-9093}
```

To be told about an alert, name a webhook in `.env`:

```env
ALERT_WEBHOOK_URL=https://example.org/hooks/wactorz
# Optional. Sent as "Authorization: Bearer <token>".
ALERT_WEBHOOK_TOKEN=
```

and restart the service (`docker compose --profile python up -d alertmanager`). Every alert, and its resolution, is then sent there as an HTTP `POST` carrying [Alertmanager's JSON](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config). The method and the body are Alertmanager's and are not settings; the token is the one credential it sends. Alerts with the same name are grouped into one notification, sent 30 seconds after the first fires and repeated every 4 hours while it lasts.

The address and the token are written to files inside the container, in memory, and the configuration names those files, so neither shows on Alertmanager's status page.

For anything a single webhook cannot express, such as email, Slack, several receivers, custom headers or routing by severity, write your own configuration to `infra/alertmanager/alertmanager.yml`. It is used exactly as written, the two settings above are then not read, and the file is ignored by git because it usually holds a credential. The [Alertmanager documentation](https://prometheus.io/docs/alerting/latest/configuration/) describes the format.

Alertmanager's page has no login and can silence an alert, so it is published to this host only, like Prometheus.
