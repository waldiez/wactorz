"""
wactorz.agents.timeseries_collector — Background agent that collects device data.

Subscribes to sensor, detection, and HA state-change MQTT topics and writes
every message to the time-series tables in SQLite. No LLM involved — pure
append-only data collection.

Starts automatically with the system (add to cli.py supervision tree).
Data is queryable by any agent via agent.query_ts().

Usage:
    collector = TimeSeriesCollector(
        persistence_dir="./state",
        topics=["sensors/#", "custom/detections/#", "homeassistant/state_changes/#"],
        batch_interval=5.0,    # flush every 5 seconds
        retention_days=90,     # auto-prune after 90 days
    )
"""

import asyncio
import json
import logging
import time
from typing import Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)


class TimeSeriesCollector(Actor):
    """
    Background agent that subscribes to MQTT topics and writes device data
    to SQLite time-series tables.

    Batches writes for performance — flushes every batch_interval seconds
    or when the batch reaches batch_size, whichever comes first.
    """

    DESCRIPTION = (
        "Collects device data from MQTT topics and stores in SQLite "
        "for historical queries and ML training"
    )
    CAPABILITIES = ["timeseries", "data_collection", "sensor_history", "ml_data"]

    def __init__(
        self,
        topics: Optional[list[str]] = None,
        batch_interval: float = 5.0,
        batch_size: int = 200,
        retention_days: int = 90,
        prune_interval_hours: float = 6.0,
        **kwargs,
    ):
        kwargs.setdefault("name", "timeseries-collector")
        super().__init__(**kwargs)

        self._topics = topics or [
            "sensors/#",
            "custom/detections/#",
            "custom/sensors/#",
            "homeassistant/state_changes/#",
            "sinergym/env/+/observation",        # Sinergym step observations
            "sinergym/env/+/episode",            # Sinergym episode start/end events
        ]
        self._batch_interval = batch_interval
        self._batch_size = batch_size
        self._retention_days = retention_days
        self._prune_interval = prune_interval_hours * 3600

        # Write buffer
        self._sensor_buffer: list[tuple] = []
        self._detection_buffer: list[tuple] = []
        self._ha_buffer: list[tuple] = []

        # Stats
        self._total_written = 0
        self._total_received = 0
        self._last_prune = 0.0

        self.protected = True  # don't let the user stop this

    async def on_start(self):
        self._tasks.append(asyncio.create_task(self._mqtt_subscriber()))
        self._tasks.append(asyncio.create_task(self._flush_loop()))
        self._tasks.append(asyncio.create_task(self._prune_loop()))

        await self.publish_manifest(
            description=self.DESCRIPTION,
            capabilities=self.CAPABILITIES,
        )
        logger.info(
            f"[{self.name}] Started — subscribing to {len(self._topics)} topic pattern(s), "
            f"batch_interval={self._batch_interval}s, retention={self._retention_days}d"
        )

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            payload = msg.payload or {}
            if isinstance(payload, dict):
                action = payload.get("action", "") or payload.get("text", "")

                # Extract task ID for proper response routing
                # delegate_task uses the task string as the future key,
                # and handle_message resolves it via _task_id or task in the response
                _task_id = payload.get("_task_id") or payload.get("task") or action

                if action == "stats":
                    from ..core.persistence import get_db
                    db = get_db()
                    stats = db.stats() if db else {}
                    result = {
                        "_task_id": _task_id,
                        "task": _task_id,
                        "result": (
                            f"Time-series collector stats:\n"
                            f"  Received: {self._total_received} messages\n"
                            f"  Written:  {self._total_written} rows\n"
                            f"  Buffers:  sensor={len(self._sensor_buffer)}, "
                            f"detection={len(self._detection_buffer)}, "
                            f"ha={len(self._ha_buffer)}\n"
                            f"  Tables:   {stats}\n"
                            f"  Retention: {self._retention_days} days"
                        ),
                        "total_received": self._total_received,
                        "total_written": self._total_written,
                        "buffer_sizes": {
                            "sensor": len(self._sensor_buffer),
                            "detection": len(self._detection_buffer),
                            "ha": len(self._ha_buffer),
                        },
                        "table_rows": stats,
                        "retention_days": self._retention_days,
                    }
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, result)

                elif action == "prune":
                    pruned = self._do_prune()
                    result = {
                        "_task_id": _task_id,
                        "task": _task_id,
                        "result": f"Pruned {pruned} rows older than {self._retention_days} days.",
                        "pruned_rows": pruned,
                    }
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, result)

                else:
                    # Unknown action — still respond so the caller doesn't hang
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, {
                            "_task_id": _task_id,
                            "task": _task_id,
                            "result": (
                                f"Unknown action: '{action}'. "
                                f"Available actions: stats, prune"
                            ),
                        })

    # ── MQTT Subscriber ────────────────────────────────────────────────────

    async def _mqtt_subscriber(self):
        """Subscribe to all configured topics and buffer incoming messages."""
        try:
            import aiomqtt
        except ImportError:
            logger.error(f"[{self.name}] aiomqtt not available — collector disabled")
            return

        _last_mqtt_exc: str | None = None
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    for pattern in self._topics:
                        await client.subscribe(pattern)
                    logger.info(f"[{self.name}] Subscribed to: {self._topics}")
                    _last_mqtt_exc = None

                    async for msg in client.messages:
                        try:
                            topic = str(msg.topic)
                            payload = json.loads(msg.payload.decode())
                            self._total_received += 1
                            self._route_message(topic, payload)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass  # skip non-JSON messages
                        except Exception as e:
                            logger.debug(f"[{self.name}] Message processing error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    exc_str = str(e)
                    if exc_str != _last_mqtt_exc:
                        logger.warning(f"[{self.name}] MQTT error: {e}. Reconnecting in 5s...")
                        _last_mqtt_exc = exc_str
                    else:
                        logger.debug(f"[{self.name}] MQTT still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    def _route_message(self, topic: str, payload: dict):
        """Route an MQTT message to the correct buffer based on topic."""
        now = time.time()

        if not isinstance(payload, dict):
            return

        # ── Detection messages ─────────────────────────────────────────
        if "detections" in topic or "detection" in topic:
            # Handle both single and batch detections
            detections = payload.get("detections", [])
            if not detections and "class" in payload:
                detections = [payload]
            agent = payload.get("agent", topic.split("/")[-1] if "/" in topic else "")
            for det in detections:
                self._detection_buffer.append((
                    now,
                    agent,
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
                self._sensor_buffer.append((
                    now, topic, entity, "reward",
                    float(reward), "", "",
                    f"sinergym-{mode}", "",
                ))

            # Store each obs dimension as obs_0, obs_1, ...
            obs = payload.get("obs", [])
            if isinstance(obs, list):
                for i, val in enumerate(obs):
                    if isinstance(val, (int, float)):
                        self._sensor_buffer.append((
                            now, topic, entity, f"obs_{i}",
                            float(val), "", "",
                            f"sinergym-{mode}", "",
                        ))

            # Store each action dimension
            action = payload.get("action", [])
            if isinstance(action, list):
                for i, val in enumerate(action):
                    if isinstance(val, (int, float)):
                        self._sensor_buffer.append((
                            now, topic, entity, f"action_{i}",
                            float(val), "", "",
                            f"sinergym-{mode}", "",
                        ))

            # Store step and episode as metadata
            self._sensor_buffer.append((
                now, topic, entity, "step",
                float(step), "", "", f"sinergym-{mode}", "",
            ))
            self._sensor_buffer.append((
                now, topic, entity, "episode",
                float(episode), "", "", f"sinergym-{mode}", "",
            ))

            # Store info dict fields (energy, comfort, etc.)
            info = payload.get("info", {})
            if isinstance(info, dict):
                for k, v in info.items():
                    if isinstance(v, (int, float)):
                        self._sensor_buffer.append((
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
                        self._sensor_buffer.append((
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

            self._ha_buffer.append((
                now, entity_id, old_val, state_val, domain, attrs, context,
            ))

        # ── Sensor data (everything else) ──────────────────────────────
        else:
            entity_id = payload.get("entity_id", "")
            agent = payload.get("agent", "")
            node = payload.get("node", "")

            # Extract each numeric field as a separate row
            for field_name, value in payload.items():
                if field_name.startswith("_"):
                    continue
                if field_name in ("agent", "node", "entity_id", "timestamp", "ts"):
                    continue

                if isinstance(value, (int, float)):
                    self._sensor_buffer.append((
                        now, topic, entity_id, field_name,
                        float(value), "", "",
                        agent, node,
                    ))
                elif isinstance(value, str) and value not in ("", "null"):
                    # Non-numeric but potentially useful (on/off, states)
                    self._sensor_buffer.append((
                        now, topic, entity_id, field_name,
                        None, value, "",
                        agent, node,
                    ))

    # ── Flush loop ─────────────────────────────────────────────────────────

    async def _flush_loop(self):
        """Periodically flush buffered data to SQLite."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(self._batch_interval)
                self._flush()
            except asyncio.CancelledError:
                self._flush()  # final flush on stop
                break
            except Exception as e:
                logger.error(f"[{self.name}] Flush error: {e}")

    def _flush(self):
        """Write all buffered data to SQLite."""
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            return

        written = 0

        if self._sensor_buffer:
            try:
                db.write_sensor_batch(self._sensor_buffer)
                written += len(self._sensor_buffer)
                self._sensor_buffer.clear()
            except Exception as e:
                logger.error(f"[{self.name}] Sensor flush failed: {e}")

        if self._detection_buffer:
            try:
                for row in self._detection_buffer:
                    db.write_detection(*row)
                written += len(self._detection_buffer)
                self._detection_buffer.clear()
            except Exception as e:
                logger.error(f"[{self.name}] Detection flush failed: {e}")

        if self._ha_buffer:
            try:
                for row in self._ha_buffer:
                    db.write_ha_state(*row)
                written += len(self._ha_buffer)
                self._ha_buffer.clear()
            except Exception as e:
                logger.error(f"[{self.name}] HA state flush failed: {e}")

        if written:
            self._total_written += written
            logger.debug(f"[{self.name}] Flushed {written} rows (total: {self._total_written})")

    # ── Retention pruning ──────────────────────────────────────────────────

    async def _prune_loop(self):
        """Periodically prune old data beyond retention window."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(self._prune_interval)
                self._do_prune()
            except asyncio.CancelledError:
                break

    def _do_prune(self) -> int:
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            return 0
        return db.prune_old_data(self._retention_days)

    async def on_stop(self):
        self._flush()  # final flush