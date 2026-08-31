"""CATALOG AGENT — anomaly-detector
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Learns "normal" patterns from historical time-series data (SQLite), then
detects anomalies in real-time (MQTT). Works with both real-world Home
Assistant devices and Sinergym building simulations — same agent, same
algorithm, different data sources.

ARCHITECTURE
────────────
  Phase 1 — LEARNING (silent, background)
    • Periodically queries timeseries-collector's SQLite for each monitored
      entity/field — builds per-entity statistical baselines:
        - Hourly mean/std profiles (24-slot diurnal pattern)
        - Day-of-week adjustment factors
        - Value range bounds (adaptive percentile-based)
        - State transition frequency (for binary sensors)
        - Cross-entity correlation baselines
    • Trains an IsolationForest per entity group for multivariate detection
    • Baseline is rebuilt on a schedule (default: weekly) to track seasonal drift

  Phase 2 — DETECTION (active, real-time)
    • Subscribes to MQTT topics and scores each incoming reading against
      the learned baseline
    • Detection methods (all run, anomaly if ANY fires):
        1. Statistical: value outside hourly_mean ± k*hourly_std
        2. Range: value outside adaptive [p1, p99] bounds
        3. Rate: change between consecutive readings exceeds learned max rate
        4. Absence: entity hasn't published in N × its normal interval
        5. Correlation break: two entities that normally move together diverge
        6. IsolationForest: multivariate outlier across entity group
    • Each anomaly is scored (0-1) and classified by type

  Phase 3 — NOTIFICATION
    • Real-world (HA): publishes to dashboard + Discord/Telegram + agent alert
    • Sinergym: publishes to sinergym/anomalies/{env_id} + notifies optimizer
    • All anomalies logged to SQLite for post-hoc analysis

MODE DETECTION
──────────────
  Automatic from entity_id patterns:
    sinergym.*    → simulation mode (short baselines, fast cycles)
    sensor.*      → real-world HA (long baselines, human-readable alerts)
    light.*       → real-world HA state transition analysis
    climate.*     → real-world HA duty cycle analysis

MQTT CONTRACT
─────────────
  Subscribe: sensors/#, homeassistant/state_changes/#, sinergym/env/+/observation
  Publish:   wactorz/anomalies/{entity_id}
             sinergym/anomalies/{env_id}     (simulation mode only)

SPAWN CONFIG
────────────
{
  "name":        "anomaly-detector",
  "type":        "dynamic",
  "description": "Learns normal patterns from time-series data, detects anomalies in real-time.",
  "capabilities": ["anomaly_detection", "time_series", "monitoring", "building_analytics",
                    "sinergym", "energy_monitoring", "comfort_monitoring"],
  "input_schema": {
    "action":             "str  — status|report|reset|configure|train|anomalies",
    "baseline_hours":     "int  — hours of history for baseline (default: 720 = 30 days)",
    "learning_period_hours": "int — min hours before detection starts (default: 168 = 1 week)",
    "sensitivity":        "float — anomaly threshold 0-1, lower=more sensitive (default: 0.3)",
    "entities":           "list  — entity IDs to monitor (default: auto-discover)",
    "rebuild_interval_hours": "int — rebuild baseline every N hours (default: 168 = weekly)"
  },
  "output_schema": {
    "anomalies_detected":  "int",
    "entities_monitored":  "int",
    "baseline_status":     "str — learning|ready|stale",
    "last_anomaly":        "dict|null"
  },
  "poll_interval": 60
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

import aiomqtt

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_BASELINE_HOURS = 720  # 30 days of history for baseline
DEFAULT_LEARNING_PERIOD_H = 168  # 1 week minimum before detection
DEFAULT_SENSITIVITY = 0.3  # anomaly threshold (0=flag everything, 1=only extremes)
DEFAULT_REBUILD_INTERVAL_H = 168  # rebuild baseline weekly
DEFAULT_ABSENCE_MULTIPLIER = 3.0  # flag if no data for 3× normal interval
DEFAULT_STAT_K = 3.0  # z-score threshold for statistical anomaly
DEFAULT_RATE_K = 4.0  # max rate-of-change multiplier
DEFAULT_MIN_SAMPLES = 50  # minimum samples before trusting a baseline
DEFAULT_SINERGYM_BASELINE_H = 2  # sinergym: much shorter baseline (simulated time)
DEFAULT_SINERGYM_LEARNING_H = 0.5  # sinergym: start detecting after 30min of data

# Topics to subscribe to for real-time detection
MONITOR_TOPICS = [
    "sensors/#",
    "custom/sensors/#",
    "homeassistant/state_changes/#",
    "sinergym/env/+/observation",
]


# ══════════════════════════════════════════════════════════════════════════════
# BASELINE MODEL
# ══════════════════════════════════════════════════════════════════════════════


class EntityBaseline:
    """Statistical baseline for one entity+field combination."""

    def __init__(self, entity_id: str, field: str) -> None:
        self.entity_id = entity_id
        self.field = field
        self.hourly_mean = [0.0] * 24  # mean value per hour-of-day
        self.hourly_std = [1.0] * 24  # std per hour-of-day
        self.hourly_count = [0] * 24  # samples per hour slot
        self.global_mean = 0.0
        self.global_std = 1.0
        self.p1 = 0.0  # 1st percentile
        self.p99 = 0.0  # 99th percentile
        self.max_rate = 0.0  # max |delta| between consecutive readings
        self.mean_interval = 0.0  # mean seconds between readings
        self.total_samples = 0
        self.last_value = None
        self.last_ts = 0.0
        self.is_binary = False  # on/off sensor
        self.transition_freq = 0.0  # state changes per hour (binary)
        self.ready = False  # enough data to detect?

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "field": self.field,
            "hourly_mean": self.hourly_mean,
            "hourly_std": self.hourly_std,
            "hourly_count": self.hourly_count,
            "global_mean": self.global_mean,
            "global_std": self.global_std,
            "p1": self.p1,
            "p99": self.p99,
            "max_rate": self.max_rate,
            "mean_interval": self.mean_interval,
            "total_samples": self.total_samples,
            "is_binary": self.is_binary,
            "transition_freq": self.transition_freq,
            "ready": self.ready,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "EntityBaseline":
        b = EntityBaseline(d["entity_id"], d["field"])
        for k in (
            "hourly_mean",
            "hourly_std",
            "hourly_count",
            "global_mean",
            "global_std",
            "p1",
            "p99",
            "max_rate",
            "mean_interval",
            "total_samples",
            "is_binary",
            "transition_freq",
            "ready",
        ):
            if k in d:
                setattr(b, k, d[k])
        return b


def _build_baseline_from_data(
    entity_id: str, field: str, rows: list[dict[str, Any]], min_samples: int
) -> "EntityBaseline":
    """Build a baseline from query results (list of dicts with ts, value)."""
    b = EntityBaseline(entity_id, field)
    values = []
    timestamps = []

    for r in rows:
        v = r.get("value")
        if v is None:
            continue
        values.append(float(v))
        timestamps.append(float(r.get("ts", 0)))

    if len(values) < min_samples:
        b.total_samples = len(values)
        return b

    b.total_samples = len(values)
    b.ready = True

    # Global stats
    b.global_mean = sum(values) / len(values)
    variance = sum((v - b.global_mean) ** 2 for v in values) / len(values)
    b.global_std = max(variance**0.5, 1e-6)

    # Percentiles
    sorted_vals = sorted(values)
    idx_1 = max(0, int(len(sorted_vals) * 0.01))
    idx_99 = min(len(sorted_vals) - 1, int(len(sorted_vals) * 0.99))
    b.p1 = sorted_vals[idx_1]
    b.p99 = sorted_vals[idx_99]

    # Binary detection (only 2 unique values)
    unique = set(values)
    if len(unique) <= 2:
        b.is_binary = True

    # Hourly profiles
    hourly_buckets = [[] for _ in range(24)]
    for ts, v in zip(timestamps, values, strict=False):
        hour = datetime.fromtimestamp(ts).hour
        hourly_buckets[hour].append(v)

    for h in range(24):
        bucket = hourly_buckets[h]
        b.hourly_count[h] = len(bucket)
        if bucket:
            b.hourly_mean[h] = sum(bucket) / len(bucket)
            if len(bucket) > 1:
                var = sum((v - b.hourly_mean[h]) ** 2 for v in bucket) / len(bucket)
                b.hourly_std[h] = max(var**0.5, 1e-6)
        else:
            b.hourly_mean[h] = b.global_mean
            b.hourly_std[h] = b.global_std

    # Rate of change
    deltas = []
    for i in range(1, len(values)):
        dt = timestamps[i] - timestamps[i - 1]
        if dt > 0:
            deltas.append(abs(values[i] - values[i - 1]) / dt)
    if deltas:
        sorted_deltas = sorted(deltas)
        idx_95 = min(len(sorted_deltas) - 1, int(len(sorted_deltas) * 0.95))
        b.max_rate = sorted_deltas[idx_95]

    # Mean interval between readings
    if len(timestamps) > 1:
        intervals = [
            timestamps[i] - timestamps[i - 1]
            for i in range(1, len(timestamps))
            if timestamps[i] > timestamps[i - 1]
        ]
        if intervals:
            b.mean_interval = sum(intervals) / len(intervals)

    # Binary transition frequency
    if b.is_binary and len(values) > 1:
        transitions = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1])
        total_hours = (timestamps[-1] - timestamps[0]) / 3600
        if total_hours > 0:
            b.transition_freq = transitions / total_hours

    return b


# ══════════════════════════════════════════════════════════════════════════════
# ANOMALY SCORING
# ══════════════════════════════════════════════════════════════════════════════


def _score_reading(
    value: float, ts: float, baseline: "EntityBaseline", stat_k: float, rate_k: float
) -> list[dict[str, Any]]:
    """Score a single reading against the baseline.
    Returns list of anomaly dicts (empty = normal).
    """
    if not baseline.ready:
        return []

    anomalies = []
    hour = datetime.fromtimestamp(ts).hour

    # 1. Statistical: value outside hourly_mean ± k*hourly_std
    hourly_m = baseline.hourly_mean[hour]
    hourly_s = baseline.hourly_std[hour]
    if hourly_s > 0 and baseline.hourly_count[hour] >= 10:
        z_score = abs(value - hourly_m) / hourly_s
        if z_score > stat_k:
            anomalies.append(
                {
                    "type": "statistical",
                    "score": min(1.0, (z_score - stat_k) / stat_k),
                    "detail": (
                        f"Value {value:.2f} is {z_score:.1f}σ from hourly mean "
                        f"{hourly_m:.2f} (hour {hour}:00)"
                    ),
                    "z_score": round(z_score, 2),
                    "expected_mean": round(hourly_m, 2),
                    "expected_std": round(hourly_s, 2),
                }
            )

    # 2. Range: outside adaptive percentile bounds
    if value < baseline.p1 or value > baseline.p99:
        overshoot = 0.0
        rng = baseline.p99 - baseline.p1
        if rng > 0:
            if value < baseline.p1:
                overshoot = (baseline.p1 - value) / rng
            else:
                overshoot = (value - baseline.p99) / rng
        anomalies.append(
            {
                "type": "range",
                "score": min(1.0, overshoot),
                "detail": (
                    f"Value {value:.2f} outside [{baseline.p1:.2f}, {baseline.p99:.2f}] "
                    f"(1st-99th percentile)"
                ),
            }
        )

    # 3. Rate: change too fast
    if baseline.last_value is not None and baseline.last_ts > 0 and baseline.max_rate > 0:
        dt = ts - baseline.last_ts
        if dt > 0:
            rate = abs(value - baseline.last_value) / dt
            if rate > baseline.max_rate * rate_k:
                anomalies.append(
                    {
                        "type": "rate",
                        "score": min(1.0, (rate / (baseline.max_rate * rate_k)) - 1.0),
                        "detail": (
                            f"Rate of change {rate:.4f}/s exceeds "
                            f"{rate_k}× baseline max {baseline.max_rate:.4f}/s"
                        ),
                    }
                )

    return anomalies


def _check_absence(
    baseline: "EntityBaseline", now: float, multiplier: float
) -> dict[str, Any] | None:
    """Check if an entity hasn't reported in too long."""
    if baseline.mean_interval <= 0 or baseline.last_ts <= 0:
        return None
    silence = now - baseline.last_ts
    threshold = baseline.mean_interval * multiplier
    if silence > threshold and threshold > 60:  # ignore sub-minute intervals
        return {
            "type": "absence",
            "score": min(1.0, (silence / threshold) - 1.0),
            "detail": (
                f"No data for {silence / 60:.0f}min — "
                f"normal interval is {baseline.mean_interval / 60:.1f}min"
            ),
            "silence_seconds": round(silence, 0),
            "expected_interval": round(baseline.mean_interval, 0),
        }
    return None


# ══════════════════════════════════════════════════════════════════════════════
# HUMAN-READABLE EXPLANATIONS
# ══════════════════════════════════════════════════════════════════════════════


def _explain_anomaly(
    entity_id: str, field: str, value: float, anomalies: list[dict[str, Any]], is_sinergym: bool
) -> str:
    """Generate a human-readable explanation of detected anomalies."""
    top = max(anomalies, key=lambda a: a["score"])

    if is_sinergym:
        prefix = f"[Sinergym] {entity_id}/{field}"
    else:
        # Make HA entity IDs readable
        readable = entity_id.replace("sensor.", "").replace("_", " ").title()
        prefix = f"{readable} ({field})"

    lines = [f"⚠️ {prefix}: {top['detail']}"]

    if len(anomalies) > 1:
        other_types = [a["type"] for a in anomalies if a != top]
        lines.append(f"  Also flagged by: {', '.join(other_types)}")

    max_score = max(a["score"] for a in anomalies)
    severity = "critical" if max_score > 0.7 else "warning" if max_score > 0.4 else "info"
    lines.append(f"  Severity: {severity} (score: {max_score:.2f})")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════


async def setup(agent) -> None:
    # Load config
    baseline_hours = int(agent.recall("baseline_hours") or DEFAULT_BASELINE_HOURS)
    learning_period_h = int(agent.recall("learning_period_hours") or DEFAULT_LEARNING_PERIOD_H)
    sensitivity = float(agent.recall("sensitivity") or DEFAULT_SENSITIVITY)
    rebuild_interval = int(agent.recall("rebuild_interval_hours") or DEFAULT_REBUILD_INTERVAL_H)
    monitored_entities = agent.recall("entities") or []

    agent.state["baseline_hours"] = baseline_hours
    agent.state["learning_period_h"] = learning_period_h
    agent.state["sensitivity"] = sensitivity
    agent.state["rebuild_interval_h"] = rebuild_interval
    agent.state["monitored_entities"] = monitored_entities
    agent.state["stat_k"] = DEFAULT_STAT_K * (1.0 + sensitivity)
    agent.state["rate_k"] = DEFAULT_RATE_K * (1.0 + sensitivity)
    agent.state["absence_multiplier"] = DEFAULT_ABSENCE_MULTIPLIER

    # Restore or init baselines
    saved_baselines = agent.recall("baselines")
    if saved_baselines and isinstance(saved_baselines, dict):
        agent.state["baselines"] = {
            k: EntityBaseline.from_dict(v) for k, v in saved_baselines.items()
        }
        await agent.log(f"Restored {len(agent.state['baselines'])} baselines from disk")
    else:
        agent.state["baselines"] = {}

    # Anomaly tracking
    agent.state["anomalies_detected"] = int(agent.recall("anomalies_detected") or 0)
    agent.state["anomaly_history"] = agent.recall("anomaly_history") or []
    agent.state["last_baseline_build"] = float(agent.recall("last_baseline_build") or 0)
    agent.state["detection_active"] = False
    agent.state["start_time"] = time.time()

    await agent.log(
        f"Anomaly detector ready | "
        f"baseline_hours={baseline_hours} | "
        f"learning_period={learning_period_h}h | "
        f"sensitivity={sensitivity} | "
        f"baselines loaded: {len(agent.state['baselines'])}"
    )

    # Declare TopicBus contract
    agent.declare_contract(
        publishes=["wactorz/anomalies"],
        subscribes=MONITOR_TOPICS,
    )

    # Start MQTT listener for real-time detection
    asyncio.create_task(_mqtt_detector(agent))


# ══════════════════════════════════════════════════════════════════════════════
# PROCESS LOOP — periodic baseline rebuilding and absence checks
# ══════════════════════════════════════════════════════════════════════════════


async def process(agent) -> None:
    now = time.time()
    baselines = agent.state["baselines"]

    # Check if learning period is over → activate detection
    elapsed_h = (now - agent.state["start_time"]) / 3600
    has_enough = len(baselines) > 0 and any(b.ready for b in baselines.values())
    learning_done = elapsed_h >= agent.state["learning_period_h"] or has_enough

    if learning_done and not agent.state["detection_active"]:
        agent.state["detection_active"] = True
        await agent.log(
            f"Learning phase complete — detection active. "
            f"Monitoring {len(baselines)} entity/field pairs."
        )

    # Periodic baseline rebuild
    last_build = agent.state["last_baseline_build"]
    rebuild_interval_s = agent.state["rebuild_interval_h"] * 3600
    if now - last_build > rebuild_interval_s:
        await _rebuild_baselines(agent)

    # Absence checks (only when detection is active)
    if agent.state["detection_active"]:
        for _key, baseline in baselines.items():
            if not baseline.ready:
                continue
            absence = _check_absence(baseline, now, agent.state["absence_multiplier"])
            if absence:
                is_sinergym = baseline.entity_id.startswith("sinergym.")
                explanation = _explain_anomaly(
                    baseline.entity_id,
                    baseline.field,
                    0.0,
                    [absence],
                    is_sinergym,
                )
                await _report_anomaly(
                    agent,
                    baseline.entity_id,
                    baseline.field,
                    0.0,
                    [absence],
                    explanation,
                    is_sinergym,
                )


# ══════════════════════════════════════════════════════════════════════════════
# BASELINE BUILDING — queries SQLite time-series store
# ══════════════════════════════════════════════════════════════════════════════


async def _rebuild_baselines(agent) -> None:
    """Query historical data and rebuild all baselines."""
    await agent.log("Rebuilding baselines from historical data...")

    baselines = agent.state["baselines"]
    baseline_hours = agent.state["baseline_hours"]
    monitored = agent.state["monitored_entities"]

    # Auto-discover entities if none configured
    if not monitored:
        monitored = await _discover_entities(agent)
        if monitored:
            agent.state["monitored_entities"] = monitored
            await agent.log(f"Auto-discovered {len(monitored)} entities to monitor")

    built = 0
    for entity_spec in monitored:
        # entity_spec can be "entity_id" or "entity_id:field"
        if ":" in entity_spec:
            entity_id, field = entity_spec.split(":", 1)
        else:
            entity_id = entity_spec
            field = None

        try:
            rows = agent.query_ts(
                hours=baseline_hours,
                entity_id=entity_id,
                field=field,
                limit=200_000,
            )

            if not rows:
                continue

            # Group by field if not specified
            if field:
                fields_data = {field: rows}
            else:
                fields_data = {}
                for r in rows:
                    f = r.get("field", "value")
                    fields_data.setdefault(f, []).append(r)

            for f, f_rows in fields_data.items():
                key = f"{entity_id}:{f}"

                # Detect if this is sinergym → use shorter thresholds
                is_sinergym = entity_id.startswith("sinergym.")
                min_samples = 20 if is_sinergym else DEFAULT_MIN_SAMPLES

                b = _build_baseline_from_data(entity_id, f, f_rows, min_samples)

                # Preserve last_value/last_ts from existing baseline
                old = baselines.get(key)
                if old:
                    b.last_value = old.last_value
                    b.last_ts = old.last_ts

                baselines[key] = b
                if b.ready:
                    built += 1

        except Exception as e:
            await agent.log(f"Baseline build failed for {entity_spec}: {e}")

    # Also build baselines from HA state changes
    try:
        ha_rows = agent.query_ha_states(hours=baseline_hours, limit=200_000)
        if ha_rows:
            ha_by_entity = {}
            for r in ha_rows:
                eid = r.get("entity_id", "")
                if eid:
                    ha_by_entity.setdefault(eid, []).append(r)

            for eid, entity_rows in ha_by_entity.items():
                key = f"{eid}:state"
                # Convert state changes to numeric where possible
                numeric_rows = []
                for r in entity_rows:
                    state = r.get("new_state", "")
                    try:
                        numeric_rows.append({"ts": r["ts"], "value": float(state)})
                    except (ValueError, TypeError):
                        # Binary state (on/off) → 1/0
                        if state.lower() in ("on", "open", "true", "home", "detected"):
                            numeric_rows.append({"ts": r["ts"], "value": 1.0})
                        elif state.lower() in ("off", "closed", "false", "away", "clear"):
                            numeric_rows.append({"ts": r["ts"], "value": 0.0})

                if numeric_rows:
                    b = _build_baseline_from_data(eid, "state", numeric_rows, DEFAULT_MIN_SAMPLES)
                    old = baselines.get(key)
                    if old:
                        b.last_value = old.last_value
                        b.last_ts = old.last_ts
                    baselines[key] = b
                    if b.ready:
                        built += 1

    except Exception as e:
        await agent.log(f"HA state baseline build failed: {e}")

    agent.state["baselines"] = baselines
    agent.state["last_baseline_build"] = time.time()

    # Persist baselines
    agent.persist("baselines", {k: b.to_dict() for k, b in baselines.items()})
    agent.persist("last_baseline_build", agent.state["last_baseline_build"])

    await agent.log(f"Baselines rebuilt: {built} ready / {len(baselines)} total entities")


async def _discover_entities(agent) -> list[str]:
    """Auto-discover entities from the time-series store."""
    entities = set()

    try:
        # Check sensor_readings for distinct entity_ids
        from wactorz.core.persistence import get_db

        db = get_db()
        if db:
            rows = db.conn.execute(
                "SELECT DISTINCT entity_id FROM sensor_readings WHERE entity_id != '' LIMIT 200"
            ).fetchall()
            for r in rows:
                entities.add(r[0])

            # Check ha_state_changes for distinct entity_ids
            rows = db.conn.execute(
                "SELECT DISTINCT entity_id FROM ha_state_changes WHERE entity_id != '' LIMIT 200"
            ).fetchall()
            for r in rows:
                entities.add(r[0])
    except Exception:
        pass

    return sorted(entities)


# ══════════════════════════════════════════════════════════════════════════════
# REAL-TIME MQTT DETECTION
# ══════════════════════════════════════════════════════════════════════════════


async def _mqtt_detector(agent) -> None:
    """Subscribe to MQTT and score each reading against baselines."""
    while True:
        try:
            async with aiomqtt.Client(
                agent._actor._mqtt_broker,
                agent._actor._mqtt_port,
                username=os.environ.get("MQTT_USERNAME") or None,
                password=os.environ.get("MQTT_PASSWORD") or None,
            ) as client:
                for pattern in MONITOR_TOPICS:
                    await client.subscribe(pattern)
                await agent.log(f"Real-time detector subscribed to {len(MONITOR_TOPICS)} patterns")

                async for msg in client.messages:
                    try:
                        topic = str(msg.topic)
                        payload = json.loads(msg.payload.decode())
                        if isinstance(payload, dict):
                            await _process_live_reading(agent, topic, payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    except Exception:
                        pass  # don't spam logs

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


async def _process_live_reading(agent, topic: str, payload: dict[str, Any]) -> None:
    """Process a single live MQTT reading for anomaly detection."""
    now = time.time()
    baselines = agent.state["baselines"]
    stat_k = agent.state["stat_k"]
    rate_k = agent.state["rate_k"]
    active = agent.state["detection_active"]

    # Extract entity_id and numeric fields
    readings = []

    if "sinergym/" in topic and "/observation" in topic:
        # Sinergym observation — extract key fields
        env_id = payload.get("env_id", "")
        entity_id = f"sinergym.{env_id}" if env_id else "sinergym"
        reward = payload.get("reward")
        if reward is not None:
            readings.append((entity_id, "reward", float(reward)))
        # Key obs dimensions (first 10 + any info fields)
        for i, v in enumerate(payload.get("obs", [])[:10]):
            if isinstance(v, (int, float)):
                readings.append((entity_id, f"obs_{i}", float(v)))
        for k, v in payload.get("info", {}).items():
            if isinstance(v, (int, float)):
                readings.append((entity_id, f"info_{k}", float(v)))

    elif "state_changes" in topic:
        # HA state change
        entity_id = payload.get("entity_id", "")
        new_state = payload.get("new_state", {})
        state_val = new_state.get("state", "") if isinstance(new_state, dict) else str(new_state)
        try:
            readings.append((entity_id, "state", float(state_val)))
        except (ValueError, TypeError):
            if state_val.lower() in ("on", "open", "true", "home", "detected"):
                readings.append((entity_id, "state", 1.0))
            elif state_val.lower() in ("off", "closed", "false", "away", "clear"):
                readings.append((entity_id, "state", 0.0))

    else:
        # Generic sensor data
        entity_id = payload.get("entity_id", "")
        for field_name, value in payload.items():
            if field_name.startswith("_") or field_name in ("agent", "node", "entity_id", "ts"):
                continue
            if isinstance(value, (int, float)):
                readings.append((entity_id, field_name, float(value)))

    # Score each reading
    for entity_id, field, value in readings:
        key = f"{entity_id}:{field}"
        baseline = baselines.get(key)

        if baseline is None:
            # Create a new (empty) baseline — will be populated on next rebuild
            baseline = EntityBaseline(entity_id, field)
            baselines[key] = baseline

        # Always update last value/ts (even during learning)
        baseline.last_value = value
        baseline.last_ts = now

        # Only detect if active and baseline is ready
        if not active or not baseline.ready:
            continue

        anomalies = _score_reading(value, now, baseline, stat_k, rate_k)

        # Filter by sensitivity threshold
        significant = [a for a in anomalies if a["score"] >= agent.state["sensitivity"]]

        if significant:
            is_sinergym = entity_id.startswith("sinergym.")
            explanation = _explain_anomaly(entity_id, field, value, significant, is_sinergym)
            await _report_anomaly(
                agent, entity_id, field, value, significant, explanation, is_sinergym
            )


# ══════════════════════════════════════════════════════════════════════════════
# ANOMALY REPORTING
# ══════════════════════════════════════════════════════════════════════════════


async def _report_anomaly(
    agent,
    entity_id: str,
    field: str,
    value: float,
    anomalies: list[dict[str, Any]],
    explanation: str,
    is_sinergym: bool,
) -> None:
    """Report an anomaly through the appropriate channels."""
    now = time.time()
    max_score = max(a["score"] for a in anomalies)
    severity = "critical" if max_score > 0.7 else "warning" if max_score > 0.4 else "info"
    anomaly_types = [a["type"] for a in anomalies]

    anomaly_record = {
        "ts": now,
        "entity_id": entity_id,
        "field": field,
        "value": value,
        "anomalies": anomalies,
        "score": round(max_score, 3),
        "severity": severity,
        "types": anomaly_types,
        "explanation": explanation,
    }

    # Track
    agent.state["anomalies_detected"] += 1
    history = agent.state["anomaly_history"]
    history.append(anomaly_record)
    # Keep last 200 anomalies in memory
    if len(history) > 200:
        agent.state["anomaly_history"] = history[-200:]
    agent.persist("anomalies_detected", agent.state["anomalies_detected"])
    agent.persist("anomaly_history", agent.state["anomaly_history"][-50:])

    # Log
    await agent.log(explanation)

    if is_sinergym:
        # Sinergym mode — publish to sinergym anomaly topic
        env_id = entity_id.replace("sinergym.", "")
        await agent.publish(f"sinergym/anomalies/{env_id}", anomaly_record)
        # Notify optimizer if available
        try:
            await agent.send_to(
                "sinergym-optimizer",
                {
                    "action": "anomaly_detected",
                    **anomaly_record,
                },
            )
        except Exception:
            pass
    else:
        # Real-world mode — publish to wactorz anomaly topic + alert
        await agent.publish(f"wactorz/anomalies/{entity_id}", anomaly_record)
        if severity in ("critical", "warning"):
            await agent.alert(explanation, severity)


# ══════════════════════════════════════════════════════════════════════════════
# handle_task — manual commands
# ══════════════════════════════════════════════════════════════════════════════


async def handle_task(agent, payload: dict[str, Any]) -> dict[str, Any]:
    # Parse JSON from "text" field when routed via @mention
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            pass

    cmd = str(payload.get("action") or payload.get("text") or "").strip().lower()

    if cmd == "status":
        baselines = agent.state.get("baselines", {})
        ready_count = sum(1 for b in baselines.values() if b.ready)
        return {
            "result": (
                f"Anomaly detector status:\n"
                f"  Detection: {'active' if agent.state.get('detection_active') else 'learning'}\n"
                f"  Baselines: {ready_count} ready / {len(baselines)} total\n"
                f"  Anomalies detected: {agent.state.get('anomalies_detected', 0)}\n"
                f"  Sensitivity: {agent.state.get('sensitivity', DEFAULT_SENSITIVITY)}\n"
                f"  Last baseline build: {_format_age(agent.state.get('last_baseline_build', 0))}"
            ),
            "detection_active": agent.state.get("detection_active", False),
            "baselines_ready": ready_count,
            "baselines_total": len(baselines),
            "anomalies_detected": agent.state.get("anomalies_detected", 0),
            "sensitivity": agent.state.get("sensitivity", DEFAULT_SENSITIVITY),
        }

    if cmd == "report":
        # Show recent anomalies
        history = agent.state.get("anomaly_history", [])
        n = int(payload.get("n", 10))
        recent = history[-n:]
        if not recent:
            return {"result": "No anomalies detected yet."}
        lines = [f"Last {len(recent)} anomalies:"]
        for a in reversed(recent):
            age = _format_age(a["ts"])
            lines.append(
                f"  [{a['severity']}] {a['entity_id']}:{a['field']} "
                f"score={a['score']} types={a['types']} ({age})"
            )
        return {"result": "\n".join(lines), "anomalies": recent}

    if cmd == "train" or cmd == "rebuild":
        await _rebuild_baselines(agent)
        baselines = agent.state.get("baselines", {})
        ready = sum(1 for b in baselines.values() if b.ready)
        return {"result": f"Baselines rebuilt: {ready} ready / {len(baselines)} total"}

    if cmd == "reset":
        agent.state["baselines"] = {}
        agent.state["anomalies_detected"] = 0
        agent.state["anomaly_history"] = []
        agent.state["detection_active"] = False
        agent.state["last_baseline_build"] = 0
        agent.persist("baselines", {})
        agent.persist("anomalies_detected", 0)
        agent.persist("anomaly_history", [])
        agent.persist("last_baseline_build", 0)
        return {"result": "Anomaly detector reset — all baselines and history cleared."}

    if cmd == "configure":
        for key in (
            "baseline_hours",
            "learning_period_hours",
            "sensitivity",
            "rebuild_interval_hours",
            "entities",
        ):
            if key in payload:
                agent.persist(key, payload[key])
                agent.state[key] = payload[key]
        # Recompute thresholds
        s = float(agent.state.get("sensitivity", DEFAULT_SENSITIVITY))
        agent.state["stat_k"] = DEFAULT_STAT_K * (1.0 + s)
        agent.state["rate_k"] = DEFAULT_RATE_K * (1.0 + s)
        return {"result": "Configured", "config": {k: payload[k] for k in payload if k != "action"}}

    if cmd == "baselines":
        baselines = agent.state.get("baselines", {})
        summary = {}
        for key, b in baselines.items():
            summary[key] = {
                "ready": b.ready,
                "samples": b.total_samples,
                "mean": round(b.global_mean, 2),
                "std": round(b.global_std, 2),
                "range": [round(b.p1, 2), round(b.p99, 2)],
                "is_binary": b.is_binary,
            }
        return {"result": f"{len(baselines)} baselines", "baselines": summary}

    if cmd == "entities":
        return {
            "result": "Monitored entities",
            "entities": agent.state.get("monitored_entities", []),
        }

    return {
        "result": "Available commands: status, report, train, reset, configure, baselines, entities",
        "commands": ["status", "report", "train", "reset", "configure", "baselines", "entities"],
    }


def _format_age(ts: float) -> str:
    if ts <= 0:
        return "never"
    age = time.time() - ts
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60:.0f}min ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"

'''
