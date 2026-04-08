# Prometheus Monitoring

This deliverable adds Prometheus-based monitoring for the **Python** Wactorz runtime.

## Scope

Included:

- Python REST API metrics at `/metrics`
- actor health and runtime metrics from the Python registry
- process/runtime metrics from the Python process
- Prometheus in Docker Compose
- optional Mosquitto and Fuseki availability probes controlled by `.env`

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

The app exposes these at:

```text
GET /metrics
```

### Mosquitto and Fuseki

Mosquitto and Fuseki are **optional** Prometheus targets.

They are monitored with the Blackbox Exporter:

- Mosquitto: TCP connect probe to `mosquitto:1883`
- Fuseki: HTTP probe to `http://fuseki:3030/$/ping`

This is availability monitoring, not deep service-specific exporter telemetry.

## Environment Flags

Add or adjust these in `.env`:

```env
PROMETHEUS_EXTERNAL_PORT=9090
PROMETHEUS_SCRAPE_INTERVAL=15s
PROMETHEUS_PYTHON_TARGET=wactorz-python
PROMETHEUS_MONITOR_MOSQUITTO=0
PROMETHEUS_MONITOR_FUSEKI=0
```

Notes:

- `PROMETHEUS_PYTHON_TARGET` chooses what Prometheus scrapes for Python metrics.
- If Wactorz runs in Compose, use the service name such as `wactorz-python` or `wactorz`.
- If Wactorz runs from the terminal on the host, use `host.docker.internal`.
- `PROMETHEUS_MONITOR_MOSQUITTO=1` enables the Mosquitto TCP probe.
- `PROMETHEUS_MONITOR_FUSEKI=1` enables the Fuseki health probe.

## Docker Compose

### Main stack

Use the Python profiles:

```bash
docker compose --profile python up -d
docker compose --profile python-full up -d
```

### Development stack

```bash
docker compose -f compose.dev.yaml up -d
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
docker compose --profile python up -d prometheus blackbox-exporter wactorz-python
```

### 2. Wactorz from terminal, Prometheus in Compose

Set:

```env
PROMETHEUS_PYTHON_TARGET=host.docker.internal
```

Start Wactorz locally in REST mode, then run:

```bash
docker compose --profile python up -d --no-deps prometheus blackbox-exporter
```

This starts only the monitoring containers and points Prometheus at the Wactorz process running on your host.

## Verification

### 1. Check Python metrics directly

```bash
curl -fsS http://localhost:8000/metrics | head
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

When `PROMETHEUS_MONITOR_FUSEKI=1`, Prometheus should show a `fuseki-blackbox` target.

If a flagged dependency is not running, that target will correctly show as failing.

## Alert Rules

Basic Prometheus alert rules are included for:

- Python app down
- actor heartbeat stale
- optional dependency probe failing
