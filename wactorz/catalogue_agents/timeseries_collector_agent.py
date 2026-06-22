"""
CATALOG AGENT — timeseries-collector
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
  Publish:   (none — write-only to SQLite)

SPAWN CONFIG
────────────
{
  "name":        "timeseries-collector",
  "type":        "dynamic",
  "description": "Collects device data from MQTT and stores it in SQLite for historical queries and ML training.",
  "capabilities": ["timeseries", "data_collection", "sensor_history", "ml_data",
                   "mqtt_subscriber", "monitoring"],
  "input_schema": {
    "action":               "str  — stats|prune|flush",
    "topics":               "list — MQTT topic patterns to collect",
    "batch_interval":       "float — seconds between SQLite flushes (default: 5.0)",
    "batch_size":           "int  — buffer hint before flush (default: 200)",
    "retention_days":       "int  — auto-prune older than N days (default: 90)",
    "prune_interval_hours": "float — hours between prune passes (default: 6.0)"
  },
  "output_schema": {
    "total_received": "int",
    "total_written":  "int",
    "buffer_sizes":   "dict",
    "table_rows":     "dict",
    "retention_days": "int"
  },
  "poll_interval": 3600
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import json
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
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    topics           = agent.recall("topics") or DEFAULT_TOPICS
    batch_interval   = float(agent.recall("batch_interval")       or DEFAULT_BATCH_INTERVAL)
    batch_size       = int(agent.recall("batch_size")             or DEFAULT_BATCH_SIZE)
    retention_days   = int(agent.recall("retention_days")         or DEFAULT_RETENTION_DAYS)
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

    # Declare TopicBus contract (write-only — no publishes)
    agent.declare_contract(
        publishes=[],
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
    # The background loops do the real work. This periodic poll is a
    # safety-net flush so buffered rows are never stranded if the flush
    # loop was interrupted.
    _flush(agent)


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

    return {
        "result": "Available actions: stats, prune, flush",
        "commands": ["stats", "prune", "flush"],
    }
'''
