# SPDX-License-Identifier: Apache-2.0
"""Now, per source: the newest event time a source has seen, held back a little.

``docs/design/event-time.md`` asks that where the model loop needs "now" (to
close a window, to decide a trial has run long enough, to time out a
cooldown) it reads the newest event time seen on the source rather than the
clock of the machine: during a live run that is the wall clock less a small
delay, during a replay it is the replay's own time, and the same code serves
both. The hold-back, ``lateness``, keeps a reading that arrives a little late
inside the window that is read next.

A source nothing has been recorded for has no time of its own, so ``now_of``
answers with the fallback, the wall clock unless told otherwise, and the
loop behaves as it does without a recorder. Reading the files needs pyarrow,
the ``data`` extra, so it is imported where it is used.

A time further ahead of the wall clock than `FUTURE_ALLOWANCE_S` is a device
clock that was wrong. The recorder files such a reading at its arrival, but a
file can still hold one: copied in, written by a `Writer` handed its times
directly, or by a release that took any time it was given. One reading in 2100
would otherwise be "now" for good, so "now" passes over it, and says so once.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .files import FUTURE_ALLOWANCE_S, DataError, hours_of

logger: logging.Logger = logging.getLogger(__name__)

#: The event-time column every file has.
TS = "ts"

#: How long a reading may arrive after its event time and still land in the
#: window read next: "now" is held back by this much.
DEFAULT_LATENESS_S = 60.0

#: The sources "now" has passed over a time ahead of the clock for, each said once.
_ahead_said: set[str] = set()


def _newest_in(pq: Any, paths: list[Path], latest: float | None) -> tuple[float | None, bool]:
    """The newest ``ts`` in ``paths`` that is not past ``latest``, and whether any was.

    ``pq`` is ``pyarrow.parquet``, imported by the caller, which knows it is there.
    """
    newest: float | None = None
    passed_over = False
    for path in paths:
        try:
            column = pq.read_table(path, columns=[TS]).column(TS)
        except Exception as exc:  # a file being written, or not ours
            logger.warning("[data] cannot read %s for its newest time: %s", path, exc)
            continue
        for value in column.drop_null().to_pylist():
            moment = float(value)
            if latest is not None and moment > latest:
                passed_over = True
            elif newest is None or moment > newest:
                newest = moment
    return newest, passed_over


def newest_event_time(root: Path, source: str, *, latest: float | None = None) -> float | None:
    """The newest event time recorded for ``source``, or None when it has none.

    The files are an hour of event time each, so the newest hour's files
    hold the answer; they are read for their ``ts`` column alone. With
    ``latest``, a time past it is not counted, and the newest hour that holds
    one that is answers.
    """
    return _newest_event_time(root, source, latest)[0]


def _newest_event_time(root: Path, source: str, latest: float | None) -> tuple[float | None, bool]:
    """`newest_event_time`, and whether a time past ``latest`` was passed over."""
    try:
        import pyarrow.parquet as pq  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return None, False
    try:
        hours = hours_of(root, source)
    except DataError:
        return None, False
    passed_over = False
    for start, paths in reversed(hours):
        if latest is not None and start > latest:
            passed_over = True
            continue
        newest, skipped = _newest_in(pq, paths, latest)
        passed_over = passed_over or skipped
        if newest is not None:
            return newest, passed_over
    return None, passed_over


def now_of(
    root: Path,
    source: str,
    *,
    lateness: float = DEFAULT_LATENESS_S,
    fallback: float | None = None,
) -> float:
    """What "now" is on ``source``: its newest event time less ``lateness``.

    ``fallback`` is the answer when nothing is recorded for the source; the
    wall clock when unsaid. A recorded time more than `FUTURE_ALLOWANCE_S` ahead
    of the wall clock is passed over, and said once per source in the log.
    """
    newest, passed_over = (
        _newest_event_time(root, source, time.time() + FUTURE_ALLOWANCE_S)
        if source
        else (None, False)
    )
    if passed_over and source not in _ahead_said:
        _ahead_said.add(source)
        logger.warning(
            "[data] %s has readings filed ahead of this machine's clock; its 'now' passes over "
            "them. Remove those hours under the data directory once its device clock is right.",
            source,
        )
    if newest is None:
        return time.time() if fallback is None else float(fallback)
    return newest - max(0.0, float(lateness))
