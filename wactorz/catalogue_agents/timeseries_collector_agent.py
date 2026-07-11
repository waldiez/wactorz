"""CATALOG AGENT — timeseries-collector
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Background data collector. Subscribes to sensor, detection, HA state-change
and Sinergym MQTT topics and writes every message to the SQLite time-series
tables. No LLM involved — pure append-only data collection with batched
writes and retention-based pruning.

Data written here is queryable by other agents (e.g. anomaly-detector) via
the persistence layer.

ARCHITECTURE
────────────
  • MQTT subscriber task  — buffers incoming messages by type
  • Flush task            — writes buffers to SQLite every batch_interval s
  • Prune task            — drops data older than retention_days on a schedule

Originally this ran as a supervised actor that started with the system. It is
now an on-demand catalog agent (mirroring anomaly-detector): spawn it when you
want collection running.

  @catalog spawn timeseries-collector

MQTT CONTRACT
─────────────
  Subscribe: sensors/#, custom/detections/#, custom/sensors/#,
             homeassistant/state_changes/#,
             sinergym/env/+/observation, sinergym/env/+/episode
  Publish:   agents/{id}/storage — hourly storage report
             (db size in bytes + human-readable, rows per table, retention)

SPAWN CONFIG
────────────
{
  "name":        "timeseries-collector",
  "type":        "dynamic",
  "description": "Collects device data from MQTT and stores it in SQLite for historical queries and ML training.",
  "capabilities": ["timeseries", "data_collection", "sensor_history", "ml_data",
                   "mqtt_subscriber", "monitoring"],
  "input_schema": {
    "action":               "str  — stats|prune|flush|query|configure|storage",
    "topics":               "list — MQTT topic patterns to collect",
    "batch_interval":       "float — seconds between SQLite flushes (default: 5.0)",
    "batch_size":           "int  — buffer hint before flush (default: 200)",
    "retention_days":       "float — auto-prune older than N days (default: 90; 0.5 = 12h)",
    "prune_interval_hours": "float — hours between prune passes (default: 6.0)",
    "table":                "str  — query: sensors|ha_states|detections|actuations (default: ha_states)",
    "hours":                "float — query: time window to return (default: 24)",
    "entity_id":            "str  — query: filter by entity",
    "limit":                "int  — query: max rows returned (default 1000, cap 5000)"
  },
  "output_schema": {
    "total_received": "int",
    "total_written":  "int",
    "buffer_sizes":   "dict",
    "table_rows":     "dict",
    "retention_days": "float",
    "rows":           "list — query results",
    "db_bytes":       "int  — storage report"
  },
  "poll_interval": 3600
}

TASK EXAMPLES (natural language also works via @mention)
─────────────
  {"action": "query", "table": "ha_states", "hours": 24, "entity_id": "sensor.office_temp"}
  {"action": "configure", "retention_days": 1}      — keep only the last 24 h
  {"action": "storage"}                              — how big is the DB right now
  "keep only 2 days of data"                         — parsed to configure
  "how much storage are you using?"                  — parsed to storage
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import json
import re
import time


# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_TOPICS = [
    "sensors/#",
    "custom/detections/#",
    "custom/sensors/#",
    "homeassistant/state_changes/#",
    "sinergym/env/+/observation",        # Sinergym step observations
    "sinergym/env/+/episode",            # Sinergym episode start/end events
]
DEFAULT_BATCH_INTERVAL   = 5.0      # flush every N seconds
DEFAULT_BATCH_SIZE       = 200      # buffer hint
DEFAULT_RETENTION_DAYS   = 90       # auto-prune after N days
DEFAULT_PRUNE_INTERVAL_H = 6.0      # hours between prune passes


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE ROUTING — fills the per-type buffers in agent.state
# ══════════════════════════════════════════════════════════════════════════════

def _route_message(agent, topic, payload):
    """Route an MQTT message to the correct buffer based on topic."""
    now = time.time()

    if not isinstance(payload, dict):
        return

    sensor_buf    = agent.state["sensor_buffer"]
    detection_buf = agent.state["detection_buffer"]
    ha_buf        = agent.state["ha_buffer"]

    # ── Detection messages ─────────────────────────────────────────
    if "detections" in topic or "detection" in topic:
        # Handle both single and batch detections
        detections = payload.get("detections", [])
        if not detections and "class" in payload:
            detections = [payload]
        src_agent = payload.get("agent", topic.split("/")[-1] if "/" in topic else "")
        for det in detections:
            detection_buf.append((
                now,
                src_agent,
                det.get("class", "unknown"),
                float(det.get("confidence", 0.0)),
                json.dumps(det.get("bbox", [])),
                int(det.get("frame_id", 0)),
                json.dumps({k: v for k, v in det.items()
                            if k not in ("class", "confidence", "bbox", "frame_id")}),
                payload.get("node", ""),
            ))

    # ── Sinergym observations ──────────────────────────────────────
    elif "sinergym/" in topic and "/observation" in topic:
        env_id  = payload.get("env_id", "")
        episode = int(payload.get("episode", 0))
        step    = int(payload.get("step", 0))
        reward  = payload.get("reward")
        mode    = payload.get("mode", "")
        entity  = f"sinergym.{env_id}" if env_id else "sinergym"

        # Store reward as a sensor reading
        if reward is not None:
            sensor_buf.append((
                now, topic, entity, "reward",
                float(reward), "", "",
                f"sinergym-{mode}", "",
            ))

        # Store each obs dimension as obs_0, obs_1, ...
        obs = payload.get("obs", [])
        if isinstance(obs, list):
            for i, val in enumerate(obs):
                if isinstance(val, (int, float)):
                    sensor_buf.append((
                        now, topic, entity, f"obs_{i}",
                        float(val), "", "",
                        f"sinergym-{mode}", "",
                    ))

        # Store each action dimension
        action = payload.get("action", [])
        if isinstance(action, list):
            for i, val in enumerate(action):
                if isinstance(val, (int, float)):
                    sensor_buf.append((
                        now, topic, entity, f"action_{i}",
                        float(val), "", "",
                        f"sinergym-{mode}", "",
                    ))

        # Store step and episode as metadata
        sensor_buf.append((
            now, topic, entity, "step",
            float(step), "", "", f"sinergym-{mode}", "",
        ))
        sensor_buf.append((
            now, topic, entity, "episode",
            float(episode), "", "", f"sinergym-{mode}", "",
        ))

        # Store info dict fields (energy, comfort, etc.)
        info = payload.get("info", {})
        if isinstance(info, dict):
            for k, v in info.items():
                if isinstance(v, (int, float)):
                    sensor_buf.append((
                        now, topic, entity, f"info_{k}",
                        float(v), "", "",
                        f"sinergym-{mode}", "",
                    ))

    # ── Sinergym episode events ────────────────────────────────────
    elif "sinergym/" in topic and "/episode" in topic:
        event = payload.get("event", "")
        if event == "episode_end":
            # Store episode summary as sensor readings for easy querying
            env_id = payload.get("env_id", "")
            entity = f"sinergym.{env_id}" if env_id else "sinergym"
            for field in ("total_reward", "mean_reward", "steps",
                          "total_energy_W", "comfort_violations_degC_steps",
                          "violation_timesteps", "duration_s"):
                val = payload.get(field)
                if val is not None and isinstance(val, (int, float)):
                    sensor_buf.append((
                        now, topic, entity, f"ep_{field}",
                        float(val), "", "",
                        "sinergym-bridge", "",
                    ))

    # ── HA state changes ───────────────────────────────────────────
    elif "state_changes" in topic:
        entity_id = payload.get("entity_id", "")
        new_state = payload.get("new_state", {})
        old_state = payload.get("old_state", {})

        # Handle both nested and flat formats
        if isinstance(new_state, dict):
            state_val = new_state.get("state", "")
            attrs = json.dumps(new_state.get("attributes", {}))
        else:
            state_val = str(new_state)
            attrs = "{}"

        old_val = old_state.get("state", "") if isinstance(old_state, dict) else str(old_state)
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        context = payload.get("context", {}).get("id", "") if isinstance(payload.get("context"), dict) else ""

        ha_buf.append((
            now, entity_id, old_val, state_val, domain, attrs, context,
        ))

    # ── Sensor data (everything else) ──────────────────────────────
    else:
        entity_id = payload.get("entity_id", "")
        src_agent = payload.get("agent", "")
        node = payload.get("node", "")

        # Extract each numeric field as a separate row
        for field_name, value in payload.items():
            if field_name.startswith("_"):
                continue
            if field_name in ("agent", "node", "entity_id", "timestamp", "ts"):
                continue

            if isinstance(value, (int, float)):
                sensor_buf.append((
                    now, topic, entity_id, field_name,
                    float(value), "", "",
                    src_agent, node,
                ))
            elif isinstance(value, str) and value not in ("", "null"):
                # Non-numeric but potentially useful (on/off, states)
                sensor_buf.append((
                    now, topic, entity_id, field_name,
                    None, value, "",
                    src_agent, node,
                ))


# ══════════════════════════════════════════════════════════════════════════════
# MQTT SUBSCRIBER
# ══════════════════════════════════════════════════════════════════════════════

async def _mqtt_subscriber(agent):
    """Subscribe to all configured topics and buffer incoming messages."""
    try:
        import aiomqtt
    except ImportError:
        await agent.log("aiomqtt not available — collector disabled")
        return
    import os

    topics = agent.state["topics"]
    _last_exc = None
    while True:
        try:
            async with aiomqtt.Client(
                agent._actor._mqtt_broker,
                agent._actor._mqtt_port,
                username=os.environ.get("MQTT_USERNAME") or None,
                password=os.environ.get("MQTT_PASSWORD") or None,
            ) as client:
                for pattern in topics:
                    await client.subscribe(pattern)
                await agent.log(f"Subscribed to {len(topics)} topic pattern(s): {topics}")
                _last_exc = None

                async for msg in client.messages:
                    try:
                        topic = str(msg.topic)
                        payload = json.loads(msg.payload.decode())
                        agent.state["total_received"] += 1
                        _route_message(agent, topic, payload)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass  # skip non-JSON messages
                    except Exception:
                        pass  # don't spam logs

        except asyncio.CancelledError:
            break
        except Exception as e:
            exc_str = str(e)
            if exc_str != _last_exc:
                await agent.log(f"MQTT error: {e}. Reconnecting in 5s...")
                _last_exc = exc_str
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
# FLUSH — write buffered rows to SQLite
# ══════════════════════════════════════════════════════════════════════════════

def _flush(agent):
    """Write all buffered data to SQLite."""
    from wactorz.core.persistence import get_db
    db = get_db()
    if not db:
        return

    written = 0
    sensor_buf    = agent.state["sensor_buffer"]
    detection_buf = agent.state["detection_buffer"]
    ha_buf        = agent.state["ha_buffer"]

    if sensor_buf:
        try:
            db.write_sensor_batch(sensor_buf)
            written += len(sensor_buf)
            sensor_buf.clear()
        except Exception:
            pass

    if detection_buf:
        try:
            for row in detection_buf:
                db.write_detection(*row)
            written += len(detection_buf)
            detection_buf.clear()
        except Exception:
            pass

    if ha_buf:
        try:
            for row in ha_buf:
                db.write_ha_state(*row)
            written += len(ha_buf)
            ha_buf.clear()
        except Exception:
            pass

    if written:
        agent.state["total_written"] += written


async def _flush_loop(agent):
    """Periodically flush buffered data to SQLite."""
    interval = agent.state["batch_interval"]
    while True:
        try:
            await asyncio.sleep(interval)
            _flush(agent)
        except asyncio.CancelledError:
            _flush(agent)  # final flush on stop
            break
        except Exception as e:
            await agent.log(f"Flush error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# RETENTION PRUNING
# ══════════════════════════════════════════════════════════════════════════════

def _do_prune(agent):
    from wactorz.core.persistence import get_db
    db = get_db()
    if not db:
        return 0
    return db.prune_old_data(agent.state["retention_days"])


async def _prune_loop(agent):
    """Periodically prune old data beyond the retention window."""
    interval = agent.state["prune_interval_s"]
    while True:
        try:
            await asyncio.sleep(interval)
            _do_prune(agent)
        except asyncio.CancelledError:
            break


# ══════════════════════════════════════════════════════════════════════════════
# STORAGE REPORT — published hourly on agents/{id}/storage, and on demand
# ══════════════════════════════════════════════════════════════════════════════

def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def _storage_report(agent):
    """Size of the SQLite database on disk (incl. WAL/SHM) + row counts."""
    import os
    from wactorz.core.persistence import get_db
    db = get_db()
    if not db:
        return {"error": "persistence not initialised"}
    db_bytes = 0
    base = str(getattr(db, "_path", "") or "")
    for suffix in ("", "-wal", "-shm"):
        try:
            db_bytes += os.path.getsize(base + suffix)
        except OSError:
            pass
    return {
        "db_bytes": db_bytes,
        "db_size": _human_bytes(db_bytes),
        "table_rows": db.stats(),
        "retention_days": agent.state["retention_days"],
        "total_received": agent.state["total_received"],
        "total_written": agent.state["total_written"],
        "ts": time.time(),
    }


async def _publish_storage_report(agent):
    report = _storage_report(agent)
    if "error" not in report:
        await agent.publish(f"agents/{agent.actor_id}/storage", report)
    return report


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    topics           = agent.recall("topics") or DEFAULT_TOPICS
    batch_interval   = float(agent.recall("batch_interval")       or DEFAULT_BATCH_INTERVAL)
    batch_size       = int(agent.recall("batch_size")             or DEFAULT_BATCH_SIZE)
    retention_days   = float(agent.recall("retention_days")       or DEFAULT_RETENTION_DAYS)
    prune_interval_h = float(agent.recall("prune_interval_hours") or DEFAULT_PRUNE_INTERVAL_H)

    agent.state["topics"]           = topics
    agent.state["batch_interval"]   = batch_interval
    agent.state["batch_size"]       = batch_size
    agent.state["retention_days"]   = retention_days
    agent.state["prune_interval_s"] = prune_interval_h * 3600

    # Write buffers
    agent.state["sensor_buffer"]    = []
    agent.state["detection_buffer"] = []
    agent.state["ha_buffer"]        = []

    # Stats
    agent.state["total_received"]   = 0
    agent.state["total_written"]    = 0

    # Declare TopicBus contract — publishes only the hourly storage report
    agent.declare_contract(
        publishes=[f"agents/{agent.actor_id}/storage"],
        subscribes=topics,
    )

    # Start the background workers
    asyncio.create_task(_mqtt_subscriber(agent))
    asyncio.create_task(_flush_loop(agent))
    asyncio.create_task(_prune_loop(agent))

    await agent.log(
        f"Time-series collector ready | "
        f"topics={len(topics)} | batch_interval={batch_interval}s | "
        f"retention={retention_days}d"
    )


async def process(agent):
    # The background loops do the real work. This periodic poll (hourly by
    # default: poll_interval=3600) is a safety-net flush so buffered rows are
    # never stranded, plus the hourly storage report on agents/{id}/storage.
    _flush(agent)
    try:
        await _publish_storage_report(agent)
    except Exception as exc:
        await agent.log(f"storage report failed: {exc}", level="warning")


# ══════════════════════════════════════════════════════════════════════════════
# handle_task — manual commands
# ══════════════════════════════════════════════════════════════════════════════

async def handle_task(agent, payload):
    # Parse JSON from "text" field when routed via @mention
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            pass

    action = str(payload.get("action") or payload.get("text") or "").strip().lower()

    if action == "stats":
        from wactorz.core.persistence import get_db
        db = get_db()
        stats = db.stats() if db else {}
        s = agent.state
        return {
            "result": (
                "Time-series collector stats:\n"
                f"  Received: {s['total_received']} messages\n"
                f"  Written:  {s['total_written']} rows\n"
                f"  Buffers:  sensor={len(s['sensor_buffer'])}, "
                f"detection={len(s['detection_buffer'])}, "
                f"ha={len(s['ha_buffer'])}\n"
                f"  Tables:   {stats}\n"
                f"  Retention: {s['retention_days']} days"
            ),
            "total_received": s["total_received"],
            "total_written":  s["total_written"],
            "buffer_sizes": {
                "sensor":    len(s["sensor_buffer"]),
                "detection": len(s["detection_buffer"]),
                "ha":        len(s["ha_buffer"]),
            },
            "table_rows":     stats,
            "retention_days": s["retention_days"],
        }

    if action == "prune":
        pruned = _do_prune(agent)
        return {
            "result": f"Pruned {pruned} rows older than {agent.state['retention_days']} days.",
            "pruned_rows": pruned,
        }

    if action == "flush":
        _flush(agent)
        return {
            "result": "Buffers flushed to SQLite.",
            "total_written": agent.state["total_written"],
        }

    # ── query: serve stored history to other agents (e.g. an optimizer) ────
    if action == "query":
        from wactorz.core.persistence import get_db
        db = get_db()
        if not db:
            return {"result": "Persistence not initialised — no data.", "rows": []}
        _flush(agent)  # include anything still buffered
        table = str(payload.get("table") or "ha_states").strip().lower()
        hours = float(payload.get("hours") or 24)
        limit = min(int(payload.get("limit") or 1000), 5000)
        entity = payload.get("entity_id") or None
        if table in ("sensors", "sensor", "sensor_readings"):
            rows = db.query_sensor(hours=hours, entity_id=entity,
                                   topic=payload.get("topic") or None,
                                   field=payload.get("field") or None, limit=limit)
        elif table in ("ha_states", "ha", "states", "ha_state_changes"):
            rows = db.query_ha_states(hours=hours, entity_id=entity,
                                      domain=payload.get("domain") or None, limit=limit)
        elif table in ("detections", "detection"):
            rows = db.query_detections(hours=hours, limit=limit)
        elif table in ("actuations", "actuation"):
            rows = db.query_actuations(hours=hours, entity_id=entity, limit=limit)
        else:
            return {"result": f"Unknown table '{table}'. "
                              "Use: sensors | ha_states | detections | actuations."}
        return {
            "result": f"{len(rows)} row(s) from {table} (last {hours:g} h"
                      + (f", entity={entity}" if entity else "") + ").",
            "table": table, "hours": hours, "count": len(rows), "rows": rows,
        }

    # ── configure: change retention at runtime (persists across restarts) ──
    if action == "configure":
        changed = []
        if payload.get("retention_days") is not None:
            days = float(payload["retention_days"])
            if days <= 0:
                return {"result": "retention_days must be > 0."}
            agent.state["retention_days"] = days
            agent.persist("retention_days", days)
            changed.append(f"retention_days={days:g}")
        if payload.get("prune_interval_hours") is not None:
            hrs = float(payload["prune_interval_hours"])
            if hrs <= 0:
                return {"result": "prune_interval_hours must be > 0."}
            agent.state["prune_interval_s"] = hrs * 3600
            agent.persist("prune_interval_hours", hrs)
            changed.append(f"prune_interval_hours={hrs:g}")
        if not changed:
            return {"result": "Nothing to configure. "
                              "Fields: retention_days, prune_interval_hours."}
        pruned = _do_prune(agent)  # apply the new window immediately
        return {
            "result": f"Configured: {', '.join(changed)}. "
                      f"Pruned {pruned} row(s) outside the new window.",
            "retention_days": agent.state["retention_days"],
            "pruned_rows": pruned,
        }

    # ── storage: on-demand size report (also published hourly) ─────────────
    if action == "storage":
        report = await _publish_storage_report(agent)
        if "error" in report:
            return {"result": report["error"]}
        return {
            "result": f"Database size: {report['db_size']} "
                      f"({report['db_bytes']} bytes) | rows: {report['table_rows']} | "
                      f"retention: {report['retention_days']:g} day(s).",
            **report,
        }

    # ── natural-language fallback (non-expert users via @mention) ──────────
    raw = str(payload.get("text") or action or "")
    low = raw.lower()
    m = re.search(r"(?:keep|retain|store|hold)\D{0,24}?(\d+(?:\.\d+)?)\s*"
                  r"(day|days|d\b|hour|hours|hr|hrs|h\b|week|weeks|month|months)", low)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            days = n / 24
        elif unit.startswith("w"):
            days = n * 7
        elif unit.startswith("mo"):
            days = n * 30
        else:
            days = n
        return await handle_task(agent, {"action": "configure", "retention_days": days})
    if any(k in low for k in ("storage", "how big", "db size", "disk", "space", "how much data")):
        return await handle_task(agent, {"action": "storage"})
    if any(k in low for k in ("give me", "send", "get", "query", "data of", "last ")) and \
       any(k in low for k in ("data", "history", "readings", "states")):
        hm = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hours|h\b|day|days|d\b)", low)
        hours = 24.0
        if hm:
            hours = float(hm.group(1)) * (24 if hm.group(2).startswith("d") else 1)
        return await handle_task(agent, {"action": "query", "table": "ha_states", "hours": hours})

    return {
        "result": "Available actions: stats, prune, flush, query, configure, storage. "
                  "Natural language works too — e.g. 'keep only 1 day of data', "
                  "'how much storage?', 'give me the last 6 hours of data'.",
        "commands": ["stats", "prune", "flush", "query", "configure", "storage"],
    }
'''
