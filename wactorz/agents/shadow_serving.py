# SPDX-License-Identifier: Apache-2.0
"""Running a model's shadow version beside the one served, and keeping the pairs.

A function agent serving ``NAME`` also holds ``NAME:shadow`` while that pointer
is set. After each input is answered with the active version, the same
function is called once more with a `ShadowView` of the actor: `load_model`
and `artifact_path` give the shadow version, what it publishes goes out under
``shadow/``, and `persist` does nothing, so a trial never writes the agent's
state or its topics. Each input makes one pair, kept as Parquet under
``shadow/<model>`` in the data layout (``docs/design/data-storage.md``), which
the comparison at the end of the trial reads (`wactorz.core.shadow_trial`).

The view is a stand-in for the actor, not a copy: everything it does not
redefine reaches the actor itself, so `options`, `llm`, `recall` and the name
are the real ones.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.paths import resolve_state_dir
from ..data.files import data_root, event_time_of

if TYPE_CHECKING:
    from .function_agent import FunctionAgent

logger = logging.getLogger(__name__)

#: Where a model's pairs are filed, under the data directory.
SHADOW_PREFIX = "shadow"

#: What a shadow pass publishes under, in front of the function's own topic.
SHADOW_TOPIC_PREFIX = "shadow"

#: How long pairs wait in memory before they are written, when the batch has
#: not filled: a slow source still has its pairs on disk within this.
FLUSH_EVERY_S = 30.0

#: The pip name of what the recorder needs, for the feed line when it is missing.
DATA_EXTRA = "wactorz[data]"


def shadow_source(model: str) -> str:
    """The source a model's pairs are filed under."""
    return f"{SHADOW_PREFIX}/{model}"


def shadow_topic(topic: str) -> str:
    """The topic a shadow pass publishes on instead of ``topic``."""
    return f"{SHADOW_TOPIC_PREFIX}/{topic}"


def input_sha256(payload: Any) -> str:
    """The hash that names an input: over its JSON, keys sorted, so the same input hashes the same."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_of(payload: Any, topic: str) -> str:
    """What the input is a reading of: the payload's ``source`` or ``device`` when it says, else the topic."""
    if isinstance(payload, Mapping):
        for key in ("source", "device"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return topic


def output_text(result: Any) -> str | None:
    """A function's result as a pair holds it: JSON text, or None for nothing published."""
    if result is None:
        return None
    return json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)


def pair_row(
    *,
    payload: Any,
    topic: str,
    arrived: float,
    active_version: int,
    shadow_version: int,
    active_output: Any,
    shadow_output: Any,
    active_ms: float,
    shadow_ms: float,
    shadow_error: str | None = None,
) -> dict[str, Any]:
    """One pair, as the file holds it; the columns ``docs/design/data-storage.md`` names."""
    ts, time_source = event_time_of(payload, arrived=arrived)
    return {
        "ts": ts,
        "arrived": arrived,
        "time_source": time_source,
        "input_sha256": input_sha256(payload),
        "source": source_of(payload, topic),
        "active_version": active_version,
        "shadow_version": shadow_version,
        "active_output": output_text(active_output),
        "shadow_output": output_text(shadow_output),
        "active_ms": active_ms,
        "shadow_ms": shadow_ms,
        "shadow_error": shadow_error,
    }


class ShadowView:
    """The actor as the function sees it on the shadow pass; see the module docstring."""

    def __init__(self, actor: FunctionAgent) -> None:
        self._actor = actor

    @property
    def artifacts(self) -> dict[str, dict[str, Any]]:
        """Each model as the shadow pass sees it: the version on trial where there is one."""
        return {**self._actor.artifacts, **self._actor.shadow}

    def artifact_path(self, key: str) -> str:
        entry = self._actor.shadow.get(key)
        return str(entry["path"]) if entry is not None else self._actor.artifact_path(key)

    async def load_model(self, key: str, *, allow_code: bool = False) -> Any:
        return await self._actor.load_shadow_model(key, allow_code=allow_code)

    async def publish(self, topic: str, payload: Any, **kwargs: Any) -> None:
        await self._actor.publish(shadow_topic(topic), payload, **kwargs)

    def persist(self, key: str, value: Any) -> None:
        """Nothing: a trial does not write the agent's memory."""

    def persist_bytes(self, key: str, data: bytes) -> None:
        """Nothing, as `persist`."""

    async def log(self, message: str, level: str = "info") -> None:
        await self._actor.log(f"[shadow] {message}", level)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._actor, name)


class PairRecorder:
    """Keeps the pairs of one model's trial, as Parquet under the data directory.

    The writer needs pyarrow, the ``data`` extra. Without it the trial still
    runs and publishes; the pairs are not kept, and `unavailable` says why, once
    for the feed.
    """

    def __init__(self, model: str, root: Path | None = None) -> None:
        self.model = model
        self.root = root if root is not None else data_root(resolve_state_dir())
        self._writer: Any = None
        self.unavailable: str | None = None
        self.recorded = 0

    def _open(self) -> Any:
        if self._writer is not None or self.unavailable is not None:
            return self._writer
        try:
            from ..data import parquet  # optional dependency: the `data` extra
        except ModuleNotFoundError:
            self.unavailable = f"pairs are not kept: install {DATA_EXTRA}"
            return None
        self._writer = parquet.Writer(self.root, shadow_source(self.model))
        return self._writer

    async def record(self, row: Mapping[str, Any]) -> str | None:
        """Keep ``row``, writing out what is due; why it was not kept, or None."""
        writer = self._open()
        if writer is None:
            return self.unavailable
        await asyncio.to_thread(writer.append, row)
        self.recorded += 1
        return None

    async def flush(self) -> None:
        if self._writer is not None:
            await asyncio.to_thread(self._writer.flush)

    async def close(self) -> None:
        if self._writer is not None:
            await asyncio.to_thread(self._writer.close)
            self._writer = None


async def flush_every(recorders: Mapping[str, PairRecorder], every: float = FLUSH_EVERY_S) -> None:
    """Write pending pairs out on a cadence, so a slow source's pairs reach the disk."""
    while True:
        await asyncio.sleep(every)
        for recorder in list(recorders.values()):
            try:
                await recorder.flush()
            except Exception as exc:
                logger.warning("[shadow] cannot write the pairs of %s: %s", recorder.model, exc)


def now_ms(started: float) -> float:
    """Milliseconds since ``started``, a `time.perf_counter` reading."""
    return (time.perf_counter() - started) * 1000.0
