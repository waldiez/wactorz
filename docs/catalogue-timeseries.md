# Time-Series Collector agent

`timeseries-collector` — subscribes to MQTT (sensors, detections, Home Assistant state
changes, Sinergym) and writes the data to SQLite time-series tables for historical queries
and ML training. Writes are batched and auto-pruned by a retention window. **No LLM.**

**Dependencies** (installed on first spawn): `aiomqtt`.

## Spawn

```text
@catalog spawn timeseries-collector
```

It's an on-demand agent (not started at boot). Once running it collects continuously and is
a natural companion to the [Anomaly Detector](catalogue-anomaly-detector.html).

## Commands

```text
@timeseries-collector stats     # received/written counts and per-table row counts
@timeseries-collector prune     # force a prune pass now
@timeseries-collector flush     # flush buffered rows to SQLite now
```

## Configuration

| Field | Meaning |
|-------|---------|
| `topics` | MQTT topic patterns to collect (default: `sensors/#`, detections, HA state, Sinergym) |
| `batch_interval` | seconds between SQLite flushes (default 5.0) |
| `batch_size` | buffered-row hint before a flush (default 200) |
| `retention_days` | auto-prune data older than N days (default 90) |
| `prune_interval_hours` | hours between prune passes (default 6.0) |

Returns `total_received`, `total_written`, `buffer_sizes`, `table_rows`, and `retention_days`.
