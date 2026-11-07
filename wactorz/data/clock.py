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
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .files import DataError, hours_of

logger = logging.getLogger(__name__)

#: The event-time column every file has.
TS = "ts"

#: How long a reading may arrive after its event time and still land in the
#: window read next: "now" is held back by this much.
DEFAULT_LATENESS_S = 60.0


def newest_event_time(root: Path, source: str) -> float | None:
    """The newest event time recorded for ``source``, or None when it has none.

    The files are an hour of event time each, so the newest hour's files
    hold the answer; they are read for their ``ts`` column alone.
    """
    try:
        import pyarrow.parquet as pq  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return None
    try:
        hours = hours_of(root, source)
    except DataError:
        return None
    if not hours:
        return None
    newest: float | None = None
    for path in hours[-1][1]:
        try:
            column = pq.read_table(path, columns=[TS]).column(TS)
        except Exception as exc:  # a file being written, or not ours
            logger.warning("[data] cannot read %s for its newest time: %s", path, exc)
            continue
        if column.null_count == len(column):
            continue
        found = column.drop_null().to_pylist()
        latest = max(float(v) for v in found) if found else None
        if latest is not None and (newest is None or latest > newest):
            newest = latest
    return newest


def now_of(
    root: Path,
    source: str,
    *,
    lateness: float = DEFAULT_LATENESS_S,
    fallback: float | None = None,
) -> float:
    """What "now" is on ``source``: its newest event time less ``lateness``.

    ``fallback`` is the answer when nothing is recorded for the source; the
    wall clock when unsaid.
    """
    newest = newest_event_time(root, source) if source else None
    if newest is None:
        return time.time() if fallback is None else float(fallback)
    return newest - max(0.0, float(lateness))
