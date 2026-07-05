# Anomaly Detector agent

`anomaly-detector` — learns normal patterns from time-series data (Home Assistant sensors
and Sinergym) and flags anomalies in real time using statistical (z-score), percentile
range, rate-of-change, and absence detection. Works with both real-world HA devices and
simulated building data.

**Dependencies** (installed on first spawn): `aiomqtt`, `numpy`.

## Spawn

```text
@catalog spawn anomaly-detector
```

It subscribes to live data, builds baselines over a learning period, then reports anomalies.
Pair it with the [Time-Series Collector](catalogue-timeseries.html) if you want history
retained for training.

## Commands

```text
@anomaly-detector status       # detection state + baseline readiness
@anomaly-detector report       # recent anomalies
@anomaly-detector baselines    # learned baselines
@anomaly-detector train        # (re)build baselines from history
@anomaly-detector reset
```

## Configuration

| Field | Meaning |
|-------|---------|
| `baseline_hours` | history used for a baseline (default 720 = 30 days) |
| `learning_period_hours` | minimum time before detection starts (default 168 = 1 week) |
| `sensitivity` | 0–1, lower = more sensitive (default 0.3) |
| `entities` | entity IDs to monitor (default: auto-discover) |

Returns `anomalies_detected`, `baselines_ready`, `detection_active`, and `last_anomaly`.
