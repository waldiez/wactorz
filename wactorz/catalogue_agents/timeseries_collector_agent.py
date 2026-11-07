# SPDX-License-Identifier: Apache-2.0
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

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core.mqtt import mqtt_client
from wactorz.core.persistence import WactorzDB, get_db
from wactorz.data.files import event_time_of

logger = logging.getLogger("timeseries-collector")

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_TOPICS = [
    "sensors/#",
    "custom/detections/#",
    "custom/sensors/#",
    "homeassistant/state_changes/#",
    "sinergym/env/+/observation",  # Sinergym step observations
    "sinergym/env/+/episode",  # Sinergym episode start/end events
]
DEFAULT_BATCH_INTERVAL = 5.0  # flush every N seconds
DEFAULT_BATCH_SIZE = 200  # buffer hint
DEFAULT_RETENTION_DAYS = 90  # auto-prune after N days
DEFAULT_PRUNE_INTERVAL_H = 6.0  # hours between prune passes


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE ROUTING — fills the per-type buffers in agent.state
# ══════════════════════════════════════════════════════════════════════════════


#: Rows waiting in a buffer for the next flush, each shaped as its table's columns.
Rows = list[tuple[Any, ...]]


class _Readings:
    """The sensor buffer as the routing fills it: each row filed under its event time.

    A row is built with the arrival as its first element, as rows always were;
    kept, it has the payload's event time there instead, with the arrival and
    where the time came from appended, as ``docs/design/event-time.md`` asks.
    """

    def __init__(self, buffer: Rows, arrived: float, ts: float, time_source: str) -> None:
        self._buffer = buffer
        self._arrived = arrived
        self._ts = ts
        self._time_source = time_source

    def append(self, row: tuple[Any, ...]) -> None:
        self._buffer.append((self._ts, *row[1:], self._arrived, self._time_source))


def _route_message(agent: AgentAPI, topic: str, payload: Any) -> None:
    """Route an MQTT message to the correct buffer based on topic."""
    now = time.time()

    if not isinstance(payload, dict):
        return

    ts, time_source = event_time_of(payload, arrived=now)
    sensor_buf = _Readings(agent.state["sensor_buffer"], now, ts, time_source)
    detection_buf = agent.state["detection_buffer"]
    ha_buf = agent.state["ha_buffer"]

    if "detections" in topic or "detection" in topic:
        _route_detections(detection_buf, now, topic, payload)
    elif "sinergym/" in topic and "/observation" in topic:
        _route_observation(sensor_buf, now, topic, payload)
    elif "sinergym/" in topic and "/episode" in topic:
        _route_episode(sensor_buf, now, topic, payload)
    elif "state_changes" in topic:
        _route_state_change(ha_buf, now, payload)
    else:
        _route_readings(sensor_buf, now, topic, payload)


def _route_detections(buffer: Rows, now: float, topic: str, payload: dict[str, Any]) -> None:
    """One row per detection, whether the payload carries a batch or is a single one."""
    detections = payload.get("detections", [])
    if not detections and "class" in payload:
        detections = [payload]
    src_agent = payload.get("agent", topic.split("/")[-1] if "/" in topic else "")
    for det in detections:
        buffer.append(
            (
                now,
                src_agent,
                det.get("class", "unknown"),
                float(det.get("confidence", 0.0)),
                json.dumps(det.get("bbox", [])),
                int(det.get("frame_id", 0)),
                json.dumps(
                    {
                        k: v
                        for k, v in det.items()
                        if k not in ("class", "confidence", "bbox", "frame_id")
                    }
                ),
                payload.get("node", ""),
            )
        )


def _sinergym_row(
    now: float, topic: str, entity: str, field: str, value: float | str, source: str
) -> tuple[Any, ...]:
    """A sensor row for one number a Sinergym environment reported."""
    return (now, topic, entity, field, float(value), "", "", source, "")


def _numbers(values: Any) -> list[tuple[int, float]]:
    """The positions and values of the numbers in ``values``, a list; nothing when it is not one."""
    if not isinstance(values, list):
        return []
    return [(i, val) for i, val in enumerate(values) if isinstance(val, (int, float))]


def _route_observation(buffer: _Readings, now: float, topic: str, payload: dict[str, Any]) -> None:
    """A Sinergym step: its reward, each observation and action dimension, the step, the episode and the info."""
    env_id = payload.get("env_id", "")
    episode = int(payload.get("episode", 0))
    step = int(payload.get("step", 0))
    reward = payload.get("reward")
    source = f"sinergym-{payload.get('mode', '')}"
    entity = f"sinergym.{env_id}" if env_id else "sinergym"

    if reward is not None:
        buffer.append(_sinergym_row(now, topic, entity, "reward", reward, source))
    # Each observation dimension as obs_0, obs_1, ..., and each action as action_0, ...
    for i, val in _numbers(payload.get("obs", [])):
        buffer.append(_sinergym_row(now, topic, entity, f"obs_{i}", val, source))
    for i, val in _numbers(payload.get("action", [])):
        buffer.append(_sinergym_row(now, topic, entity, f"action_{i}", val, source))
    buffer.append(_sinergym_row(now, topic, entity, "step", step, source))
    buffer.append(_sinergym_row(now, topic, entity, "episode", episode, source))
    # The info dict's numbers (energy, comfort, etc.)
    info = payload.get("info", {})
    if isinstance(info, dict):
        for k, v in info.items():
            if isinstance(v, (int, float)):
                buffer.append(_sinergym_row(now, topic, entity, f"info_{k}", v, source))


#: The fields of a Sinergym episode summary that are stored, as ``ep_<field>``.
EPISODE_FIELDS = (
    "total_reward",
    "mean_reward",
    "steps",
    "total_energy_W",
    "comfort_violations_degC_steps",
    "violation_timesteps",
    "duration_s",
)


def _route_episode(buffer: _Readings, now: float, topic: str, payload: dict[str, Any]) -> None:
    """An episode's summary when it ends, as sensor readings for easy querying."""
    if payload.get("event", "") != "episode_end":
        return
    env_id = payload.get("env_id", "")
    entity = f"sinergym.{env_id}" if env_id else "sinergym"
    for field in EPISODE_FIELDS:
        val = payload.get(field)
        if val is not None and isinstance(val, (int, float)):
            buffer.append(_sinergym_row(now, topic, entity, f"ep_{field}", val, "sinergym-bridge"))


def _route_state_change(buffer: Rows, now: float, payload: dict[str, Any]) -> None:
    """A Home Assistant state change, in its nested form or flattened."""
    entity_id = payload.get("entity_id", "")
    new_state = payload.get("new_state", {})
    old_state = payload.get("old_state", {})
    if isinstance(new_state, dict):
        state_val = new_state.get("state", "")
        attrs = json.dumps(new_state.get("attributes", {}))
    else:
        state_val = str(new_state)
        attrs = "{}"
    old_val = old_state.get("state", "") if isinstance(old_state, dict) else str(old_state)
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    context = (
        payload.get("context", {}).get("id", "") if isinstance(payload.get("context"), dict) else ""
    )
    buffer.append((now, entity_id, old_val, state_val, domain, attrs, context))


#: Payload fields that say who sent a reading and when, rather than what it read.
_NOT_READINGS = ("agent", "node", "entity_id", "timestamp", "ts")


def _route_readings(buffer: _Readings, now: float, topic: str, payload: dict[str, Any]) -> None:
    """One row per field of a sensor payload: numbers as values, other words (on/off, states) as text."""
    entity_id = payload.get("entity_id", "")
    src_agent = payload.get("agent", "")
    node = payload.get("node", "")
    for field_name, value in payload.items():
        if field_name.startswith("_") or field_name in _NOT_READINGS:
            continue
        if isinstance(value, (int, float)):
            buffer.append(
                (now, topic, entity_id, field_name, float(value), "", "", src_agent, node)
            )
        elif isinstance(value, str) and value not in ("", "null"):
            buffer.append((now, topic, entity_id, field_name, None, value, "", src_agent, node))


# ══════════════════════════════════════════════════════════════════════════════
# MQTT SUBSCRIBER
# ══════════════════════════════════════════════════════════════════════════════


async def _mqtt_subscriber(agent: AgentAPI) -> None:
    """Subscribe to all configured topics and buffer incoming messages."""
    topics = agent.state["topics"]
    _last_exc = None
    while True:
        try:
            async with mqtt_client(agent._actor._mqtt_broker, agent._actor._mqtt_port) as client:
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
                        # Per message, so at debug: a bad stream would flood the log.
                        logger.debug("could not route a message on %s", topic, exc_info=True)

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


def _flush(agent: AgentAPI) -> None:
    """Write all buffered data to SQLite."""
    db = get_db()
    if not db:
        return

    written = 0
    sensor_buf = agent.state["sensor_buffer"]
    detection_buf = agent.state["detection_buffer"]
    ha_buf = agent.state["ha_buffer"]

    if sensor_buf:
        try:
            db.write_sensor_batch(sensor_buf)
            written += len(sensor_buf)
            sensor_buf.clear()
        except Exception as e:
            logger.warning(
                "could not write %d sensor rows, kept for the next flush: %s", len(sensor_buf), e
            )

    # The per-row writes go in one transaction, so a failure partway leaves
    # none of the buffer written and the retry adds no copies.
    if detection_buf:
        try:
            with db.transaction():
                for row in detection_buf:
                    db.write_detection(*row)
            written += len(detection_buf)
            detection_buf.clear()
        except Exception as e:
            logger.warning(
                "could not write %d detection rows, kept for the next flush: %s",
                len(detection_buf),
                e,
            )

    if ha_buf:
        try:
            with db.transaction():
                for row in ha_buf:
                    db.write_ha_state(*row)
            written += len(ha_buf)
            ha_buf.clear()
        except Exception as e:
            logger.warning(
                "could not write %d Home Assistant state rows, kept for the next flush: %s",
                len(ha_buf),
                e,
            )

    if written:
        agent.state["total_written"] += written


async def _flush_loop(agent: AgentAPI) -> None:
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


def _do_prune(agent: AgentAPI) -> int:
    db = get_db()
    if not db:
        return 0
    return db.prune_old_data(agent.state["retention_days"])


async def _prune_loop(agent: AgentAPI) -> None:
    """Periodically prune old data beyond the retention window.

    The interval is read each time round, so `configure` changes it without a
    restart; and a failed pass is logged and the next one tried, since a loop
    that ended would leave the database growing with nobody told.
    """
    while True:
        try:
            await asyncio.sleep(agent.state["prune_interval_s"])
            _do_prune(agent)
        except asyncio.CancelledError:
            break
        except Exception as e:
            await agent.log(f"Prune error: {e}", level="warning")


# ══════════════════════════════════════════════════════════════════════════════
# STORAGE REPORT — published hourly on agents/{id}/storage, and on demand
# ══════════════════════════════════════════════════════════════════════════════


def _human_bytes(n: float) -> str:
    """A byte count in the largest unit that keeps it under 1024.

    `size` rather than reusing `n`, so the argument still means the argument
    further down. The last unit is returned after the loop instead of being
    special-cased inside it, which leaves every exit visible.
    """
    if n < 1024:
        return f"{int(n)} B"
    size = n / 1024
    for unit in ("KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _storage_report(agent: AgentAPI) -> dict[str, Any]:
    """Size of the SQLite database on disk (incl. WAL/SHM) + row counts."""
    db = get_db()
    if not db:
        return {"error": "persistence not initialised"}
    db_bytes = 0
    # Unset means nothing to measure; Path("") would be the working directory.
    base = str(getattr(db, "_path", "") or "")
    for suffix in ("", "-wal", "-shm") if base else ():
        try:
            db_bytes += Path(base + suffix).stat().st_size
        except OSError:
            pass  # no WAL or SHM file while nothing is open
    return {
        "db_bytes": db_bytes,
        "db_size": _human_bytes(db_bytes),
        "table_rows": db.stats(),
        "retention_days": agent.state["retention_days"],
        "total_received": agent.state["total_received"],
        "total_written": agent.state["total_written"],
        "ts": time.time(),
    }


async def _publish_storage_report(agent: AgentAPI) -> dict[str, Any]:
    report = _storage_report(agent)
    if "error" not in report:
        await agent.publish(f"agents/{agent.actor_id}/storage", report)
    return report


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════


async def setup(agent: AgentAPI) -> None:
    topics = agent.recall("topics") or DEFAULT_TOPICS
    batch_interval = float(agent.recall("batch_interval") or DEFAULT_BATCH_INTERVAL)
    batch_size = int(agent.recall("batch_size") or DEFAULT_BATCH_SIZE)
    retention_days = float(agent.recall("retention_days") or DEFAULT_RETENTION_DAYS)
    prune_interval_h = float(agent.recall("prune_interval_hours") or DEFAULT_PRUNE_INTERVAL_H)

    agent.state["topics"] = topics
    agent.state["batch_interval"] = batch_interval
    agent.state["batch_size"] = batch_size
    agent.state["retention_days"] = retention_days
    agent.state["prune_interval_s"] = prune_interval_h * 3600

    # Write buffers
    agent.state["sensor_buffer"] = []
    agent.state["detection_buffer"] = []
    agent.state["ha_buffer"] = []

    # Stats
    agent.state["total_received"] = 0
    agent.state["total_written"] = 0

    # Declare TopicBus contract — publishes only the hourly storage report
    agent.declare_contract(
        publishes=[f"agents/{agent.actor_id}/storage"],
        subscribes=topics,
    )

    # Start the background workers
    agent.run_in_background(_mqtt_subscriber(agent))
    agent.run_in_background(_flush_loop(agent))
    agent.run_in_background(_prune_loop(agent))

    await agent.log(
        f"Time-series collector ready | "
        f"topics={len(topics)} | batch_interval={batch_interval}s | "
        f"retention={retention_days}d"
    )


async def process(agent: AgentAPI) -> None:
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


def _number(value: Any) -> float | None:
    """``value`` as a float, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _task_of(payload: Any) -> dict[str, Any]:
    """The task as a dict: JSON in a mention's ``text`` read as one, anything not a dict as its action."""
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, TypeError):
            pass  # not JSON: the text is the request
    if not isinstance(payload, dict):
        payload = {"action": str(payload)}
    return payload


async def handle_task(agent: AgentAPI, payload: Any) -> dict[str, Any]:
    payload = _task_of(payload)
    action = str(payload.get("action") or payload.get("text") or "").strip().lower()
    answer = TASKS.get(action)
    if answer is not None:
        return await answer(agent, payload)
    return await _understood(agent, payload, action)


async def _stats(agent: AgentAPI, _payload: dict[str, Any]) -> dict[str, Any]:
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
        "total_written": s["total_written"],
        "buffer_sizes": {
            "sensor": len(s["sensor_buffer"]),
            "detection": len(s["detection_buffer"]),
            "ha": len(s["ha_buffer"]),
        },
        "table_rows": stats,
        "retention_days": s["retention_days"],
    }


async def _prune(agent: AgentAPI, _payload: dict[str, Any]) -> dict[str, Any]:
    pruned = _do_prune(agent)
    return {
        "result": f"Pruned {pruned} rows older than {agent.state['retention_days']} days.",
        "pruned_rows": pruned,
    }


async def _flush_now(agent: AgentAPI, _payload: dict[str, Any]) -> dict[str, Any]:
    _flush(agent)
    return {"result": "Buffers flushed to SQLite.", "total_written": agent.state["total_written"]}


#: What `query` takes as a table name, and the table each one is.
TABLES = {
    **dict.fromkeys(("sensors", "sensor", "sensor_readings"), "sensors"),
    **dict.fromkeys(("ha_states", "ha", "states", "ha_state_changes"), "ha_states"),
    **dict.fromkeys(("detections", "detection"), "detections"),
    **dict.fromkeys(("actuations", "actuation"), "actuations"),
}


def _rows_of(
    db: WactorzDB, table: str, payload: dict[str, Any], hours: float, entity: str | None, limit: int
) -> list[dict[str, Any]]:
    """The rows of ``table``, one of `TABLES`' values, as the query asks for them."""
    if table == "sensors":
        return db.query_sensor(
            hours=hours,
            entity_id=entity,
            topic=payload.get("topic") or None,
            field=payload.get("field") or None,
            limit=limit,
        )
    if table == "ha_states":
        return db.query_ha_states(
            hours=hours, entity_id=entity, domain=payload.get("domain") or None, limit=limit
        )
    if table == "detections":
        return db.query_detections(hours=hours, limit=limit)
    return db.query_actuations(hours=hours, entity_id=entity, limit=limit)


async def _query(agent: AgentAPI, payload: dict[str, Any]) -> dict[str, Any]:
    """Serve stored history to other agents (e.g. an optimizer)."""
    db = get_db()
    if not db:
        return {"result": "Persistence not initialised — no data.", "rows": []}
    _flush(agent)  # include anything still buffered
    table = str(payload.get("table") or "ha_states").strip().lower()
    try:
        hours = float(payload.get("hours") or 24)
        limit = min(int(payload.get("limit") or 1000), 5000)
    except (TypeError, ValueError):
        return {"result": "hours and limit must be a number.", "rows": []}
    entity = payload.get("entity_id") or None
    if table not in TABLES:
        return {
            "result": f"Unknown table '{table}'. "
            "Use: sensors | ha_states | detections | actuations."
        }
    rows = _rows_of(db, TABLES[table], payload, hours, entity, limit)
    return {
        "result": f"{len(rows)} row(s) from {table} (last {hours:g} h"
        + (f", entity={entity}" if entity else "")
        + ").",
        "table": table,
        "hours": hours,
        "count": len(rows),
        "rows": rows,
    }


def _positive(payload: dict[str, Any], field: str) -> float | str:
    """``payload[field]`` as a number above zero, or what is wrong with it, in words."""
    value = _number(payload[field])
    if value is None:
        return f"{field} must be a number."
    if value <= 0:
        return f"{field} must be > 0."
    return value


async def _configure(agent: AgentAPI, payload: dict[str, Any]) -> dict[str, Any]:
    """Change retention at runtime; it persists across restarts."""
    changed = []
    if payload.get("retention_days") is not None:
        days = _positive(payload, "retention_days")
        if isinstance(days, str):
            return {"result": days}
        agent.state["retention_days"] = days
        agent.persist("retention_days", days)
        changed.append(f"retention_days={days:g}")
    if payload.get("prune_interval_hours") is not None:
        hrs = _positive(payload, "prune_interval_hours")
        if isinstance(hrs, str):
            return {"result": hrs}
        agent.state["prune_interval_s"] = hrs * 3600
        agent.persist("prune_interval_hours", hrs)
        changed.append(f"prune_interval_hours={hrs:g}")
    if not changed:
        return {"result": "Nothing to configure. Fields: retention_days, prune_interval_hours."}
    pruned = _do_prune(agent)  # apply the new window immediately
    return {
        "result": f"Configured: {', '.join(changed)}. "
        f"Pruned {pruned} row(s) outside the new window.",
        "retention_days": agent.state["retention_days"],
        "pruned_rows": pruned,
    }


async def _storage(agent: AgentAPI, _payload: dict[str, Any]) -> dict[str, Any]:
    """The database's size on demand; it is also published hourly."""
    report = await _publish_storage_report(agent)
    if "error" in report:
        return {"result": report["error"]}
    return {
        "result": f"Database size: {report['db_size']} "
        f"({report['db_bytes']} bytes) | rows: {report['table_rows']} | "
        f"retention: {report['retention_days']:g} day(s).",
        **report,
    }


#: The actions a task names, and what answers each.
TASKS: dict[str, Callable[[AgentAPI, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "stats": _stats,
    "prune": _prune,
    "flush": _flush_now,
    "query": _query,
    "configure": _configure,
    "storage": _storage,
}

#: A request to keep so much data: a number and its unit.
KEEP = re.compile(
    r"(?:keep|retain|store|hold)\D{0,24}?(\d+(?:\.\d+)?)\s*"
    r"(day|days|d\b|hour|hours|hr|hrs|h\b|week|weeks|month|months)"
)


def _days(n: float, unit: str) -> float:
    """``n`` of ``unit`` (hours, days, weeks or months) in days."""
    if unit.startswith("h"):
        return n / 24
    if unit.startswith("w"):
        return n * 7
    if unit.startswith("mo"):
        return n * 30
    return n


def _hours_asked(low: str) -> float:
    """The window a request for data names, in hours: 24 when it names none."""
    hm = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hours|h\b|day|days|d\b)", low)
    if not hm:
        return 24.0
    return float(hm.group(1)) * (24 if hm.group(2).startswith("d") else 1)


async def _understood(agent: AgentAPI, payload: dict[str, Any], action: str) -> dict[str, Any]:
    """Answer a request in words (non-expert users via @mention), or say what is understood."""
    low = str(payload.get("text") or action or "").lower()
    m = KEEP.search(low)
    if m:
        days = _days(float(m.group(1)), m.group(2))
        return await handle_task(agent, {"action": "configure", "retention_days": days})
    if any(k in low for k in ("storage", "how big", "db size", "disk", "space", "how much data")):
        return await handle_task(agent, {"action": "storage"})
    if any(k in low for k in ("give me", "send", "get", "query", "data of", "last ")) and any(
        k in low for k in ("data", "history", "readings", "states")
    ):
        return await handle_task(
            agent, {"action": "query", "table": "ha_states", "hours": _hours_asked(low)}
        )
    return {
        "result": "Available actions: stats, prune, flush, query, configure, storage. "
        "Natural language works too — e.g. 'keep only 1 day of data', "
        "'how much storage?', 'give me the last 6 hours of data'.",
        "commands": ["stats", "prune", "flush", "query", "configure", "storage"],
    }
