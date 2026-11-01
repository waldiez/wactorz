# SPDX-License-Identifier: Apache-2.0
"""The recorder: keeps the readings of streams as Parquet files, where it runs.

A library agent, spawned from the catalogue on main or on a node with the
topics it records::

    {"type": "module", "target": "wactorz.agents.recorder_agent:RecorderAgent",
     "name": "recorder", "node": "pi-kitchen",
     "options": {"topics": ["sensors/imu/#", "sensors/hvac/ahu-1"], "source_field": "device"}}

Every reading on those topics is one row of its source, filed under
``<state>/data/<source>/<yyyy>/<mm>/<dd>/<hh>.parquet`` in event time by the
writer in `wactorz.data.parquet`, which flushes an hour when it turns, a batch
when it fills, and the rest on a cadence here. The source is what the payload
names in ``source_field`` (``device`` or ``source`` when unsaid), else the
topic the reading came on, with wildcard levels dropped: a subscription is
handed the payload, not the topic it matched.

On a node the files are kept under the node's state directory and the runner
ships each closed one to main (``docs/design/data-storage.md``); on main they
are the record as they are. The collector keeps writing the dashboard's live
window to SQLite; this is the model loop's store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..core.actor import Actor, Message, MessageType
from ..core.paths import resolve_state_dir
from ..data.files import DataError, check_source, data_root, event_time_of

logger = logging.getLogger(__name__)

#: What the catalogue and the dashboard call it.
NAME = "recorder"

#: The payload fields read for the source when none is named, in order.
DEFAULT_SOURCE_FIELDS = ("source", "device")

#: How often pending rows are written out, so a slow source's readings reach the disk.
FLUSH_EVERY_S = 30.0

#: The pip name of what the writer needs, for the feed line when it is missing.
DATA_EXTRA = "wactorz[data]"


def source_from_topic(topic: str) -> str:
    """A subscription's filter as a source: its levels up to the first wildcard."""
    levels: list[str] = []
    for level in topic.split("/"):
        if level in ("#", "+") or not level:
            break
        levels.append(level)
    return "/".join(levels)


def topics_of(value: Any) -> tuple[str, ...]:
    """``value`` as topic filters: one string, comma separated, or several."""
    if isinstance(value, str):
        return tuple(t.strip() for t in value.split(",") if t.strip())
    if isinstance(value, Iterable):
        return tuple(str(t).strip() for t in value if str(t).strip())
    return ()


class RecorderAgent(Actor):
    """Records the readings of ``topics`` as Parquet, one source per device or topic."""

    #: What the catalogue lists it as, and says of it.
    AGENT_NAME = NAME
    DESCRIPTION = (
        "Keeps the readings of streams as Parquet files, in event time, for training and drift."
    )
    CAPABILITIES = ("data_collection", "timeseries", "ml_data", "mqtt_subscriber")
    INPUT_SCHEMA: ClassVar[dict[str, str]] = {
        "topics": 'list — the topic filters to record, e.g. ["sensors/imu/#"] (spawn option)',
        "source_field": "str — the payload field naming the source; device or source when unsaid",
        "action": "str — flush|status, as a task",
    }
    AUTOSTART = False

    def __init__(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        actor_id: str | None = None,
        topics: Any = (),
        source_field: str = "",
        batch_rows: int | None = None,
        flush_every_s: float = FLUSH_EVERY_S,
    ) -> None:
        super().__init__(actor_id=actor_id, name=name or NAME, persistence_dir=persistence_dir)
        self.topics = topics_of(topics)
        self.source_field = source_field.strip()
        self.batch_rows = batch_rows
        self.flush_every_s = flush_every_s
        #: One writer per source, opened as its first reading arrives.
        self._writers: dict[str, Any] = {}
        self._parquet: Any = None
        self._unavailable: str | None = None
        self._flusher: asyncio.Task[Any] | None = None
        #: What this agent has recorded and refused, for its card.
        self.recorded = 0
        self.refused = 0

    @property
    def root(self) -> Path:
        """Where this agent files its sources: the data directory of the state directory it runs with."""
        runner = getattr(self, "_runner", None)
        state_dir = getattr(runner, "state_dir", None)
        return data_root(state_dir or resolve_state_dir())

    async def on_start(self) -> None:
        await self.publish_manifest(
            description=self.DESCRIPTION,
            publishes=[],
            capabilities=list(self.CAPABILITIES),
            subscribes=list(self.topics),
        )
        if not self.topics:
            await self.log("recording nothing: no topics were given", level="warning")
            return
        for topic in self.topics:
            self.subscribe(topic, self._listener(topic))
        self._flusher = self.run_detached(self._flush_loop(), name=f"{self.name}:flush")
        await self.log(f"Recording {', '.join(self.topics)} under {self.root}")

    async def on_stop(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            self._flusher = None
        await self.flush()
        await super().on_stop()

    async def handle_message(self, msg: Message) -> None:
        """A task: ``{"action": "flush"}`` writes what is pending; ``"status"`` says what was recorded."""
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        action = str(payload.get("action") or "status")
        if action == "flush":
            await self.flush()
        reply = {
            "topics": list(self.topics),
            "root": str(self.root),
            "recorded": self.recorded,
            "refused": self.refused,
            "pending": sum(w.pending for w in self._writers.values()),
            "sources": sorted(self._writers),
        }
        if self._unavailable:
            reply["unavailable"] = self._unavailable
        task_id = payload.get("_task_id")
        if task_id is not None:
            reply["_task_id"] = task_id
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, reply)

    # ── Recording ─────────────────────────────────────────────────────────

    def _listener(self, topic: str) -> Any:
        async def on_message(payload: Any) -> None:
            await self.record(payload, topic)

        return on_message

    def source_of(self, payload: Any, topic: str) -> str:
        """Where a reading is filed: the payload's named field, else the topic without its wildcards."""
        if isinstance(payload, Mapping):
            fields = (self.source_field,) if self.source_field else DEFAULT_SOURCE_FIELDS
            for key in fields:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return source_from_topic(topic)

    async def record(self, payload: Any, topic: str) -> bool:
        """File one reading; whether it was kept."""
        arrived = time.time()
        if not isinstance(payload, Mapping):
            self.refused += 1
            return False
        writer = self._writer_for(self.source_of(payload, topic))
        if writer is None:
            self.refused += 1
            return False
        ts, time_source = event_time_of(payload, arrived=arrived)
        row = {
            key: value
            for key, value in payload.items()
            if key not in ("ts", "timestamp", "arrived", "time_source")
        }
        row.update({"ts": ts, "arrived": arrived, "time_source": time_source})
        try:
            await asyncio.to_thread(writer.append, row)
        except (OSError, DataError) as exc:
            self.refused += 1
            await self.log(f"cannot record a reading of {writer.source}: {exc}", level="warning")
            return False
        self.recorded += 1
        self.metrics.messages_processed += 1
        return True

    def _writer_for(self, source: str) -> Any:
        writer = self._writers.get(source)
        if writer is not None:
            return writer
        parquet = self._module()
        if parquet is None:
            return None
        try:
            check_source(source)
        except DataError as exc:
            logger.warning("[%s] %s", self.name, exc)
            return None
        kwargs = {} if self.batch_rows is None else {"batch_rows": self.batch_rows}
        writer = parquet.Writer(self.root, source, **kwargs)
        self._writers[source] = writer
        return writer

    def _module(self) -> Any:
        """The Parquet writer's module, or None once it is known to be missing."""
        if self._parquet is not None or self._unavailable is not None:
            return self._parquet
        try:
            from ..data import parquet  # optional dependency: the `data` extra
        except ModuleNotFoundError:
            self._unavailable = f"readings are not kept: install {DATA_EXTRA}"
            logger.warning("[%s] %s", self.name, self._unavailable)
            return None
        self._parquet = parquet
        return parquet

    async def flush(self) -> list[Path]:
        """Write every pending row out; the files that made."""
        made: list[Path] = []
        for writer in list(self._writers.values()):
            try:
                made.extend(await asyncio.to_thread(writer.flush))
            except OSError as exc:
                await self.log(
                    f"cannot write the readings of {writer.source}: {exc}", level="warning"
                )
        return made

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_every_s)
            await self.flush()

    async def log(self, message: str, level: str = "info") -> None:
        """Say something on the dashboard feed, under this agent's name."""
        getattr(logger, level, logger.info)("[%s] %s", self.name, message)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )
