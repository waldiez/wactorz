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

## Recording to Parquet, for the model loop

The collector keeps the dashboard's live window in SQLite. Readings the model loop
trains on and watches for drift go to Parquet instead, one file per source and hour
under the state directory's `data/`, written by the `recorder` library agent
(`docs/design/data-storage.md`):

```text
@catalog spawn recorder {"topics": ["sensors/imu/#"], "source_field": "device"}
```

Or on a node, where it records what that node sees and the node ships each closed
file to main:

```json
{"type": "module", "target": "wactorz.agents.recorder_agent:RecorderAgent",
 "name": "recorder", "node": "pi-kitchen",
 "options": {"topics": ["sensors/imu/#"], "source_field": "device"}}
```

Each reading is a row of its source, the payload's `source` or `device` field, or the
topic without its wildcards when it names none, filed under its event time with the
arrival beside it. `wactorz-data sources`, `wactorz-data profile <source>` and
`GET /api/data/{source}/profile` say what was recorded; `wactorz-data export` writes a
window as one file for a notebook. The recorder needs the `data` extra (pyarrow).
