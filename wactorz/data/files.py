# SPDX-License-Identifier: Apache-2.0
"""The layout of the data directory, and the time a reading is filed under.

One directory per source, one per hour of event time under it, in UTC::

    <state>/data/<source>/<yyyy>/<mm>/<dd>/<hh>.parquet
    <state>/data/<source>/<yyyy>/<mm>/<dd>/<hh>-0001.parquet   # a later part of the same hour

A file is written whole and never appended to, so a reader never sees half of
one. A batch that closes before the hour turns, because it grew large or the
writer was asked to flush, is a further part of the hour, numbered after the
first; a reader takes every part of the hour together. ``docs/design/data-
storage.md`` is the decision.

A source is a topic or a device named inside a payload, with ``/`` kept as
directory levels; ``shadow/<model>`` and ``serving/<model>`` are the pairs and
logs of a model's serving. Nothing in a source may walk out of the data
directory.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.labels import event_time

#: The data directory's name under the state directory.
DATA_DIRNAME = "data"

#: What `event_time_of` reports when the time came from the payload, and when
#: it is the arrival time, so a reader can tell the two apart.
FROM_PAYLOAD = "payload"
FROM_ARRIVAL = "arrival"

#: What one level of a source may be: the characters a topic level or a device
#: name is made of, never empty, never ``.`` or ``..``.
_LEVEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")

#: The files of one hour: the hour's own and its later parts.
_PART = re.compile(r"^(?P<hour>\d{2})(?:-(?P<part>\d{4}))?\.parquet$")


class DataError(ValueError):
    """Data could not be filed or found, and why."""

    @classmethod
    def bad_source(cls, source: str) -> DataError:
        return cls(
            f"{source!r} cannot name a source: levels of letters, digits, '.', '_', ':', '@' "
            "and '-' separated by '/', none empty, none '.' or '..'"
        )

    @classmethod
    def no_event_time(cls, row: Mapping[str, Any]) -> DataError:
        return cls(f"a row has no event time 'ts': {sorted(row)}")


def data_root(state_dir: str | os.PathLike[str]) -> Path:
    """The data directory under ``state_dir``."""
    return Path(state_dir) / DATA_DIRNAME


def check_source(source: str) -> str:
    """``source`` if it can name a source; `DataError` saying why not."""
    levels = source.split("/")
    if not levels or any(not _LEVEL.match(level) or level in (".", "..") for level in levels):
        raise DataError.bad_source(source)
    return source


def source_dir(root: Path, source: str) -> Path:
    """Where ``source``'s hours are, under ``root``."""
    return root.joinpath(*check_source(source).split("/"))


def hour_of(ts: float) -> tuple[int, int, int, int]:
    """The UTC year, month, day and hour ``ts`` falls in."""
    moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return moment.year, moment.month, moment.day, moment.hour


def hour_dir(root: Path, source: str, ts: float) -> Path:
    """The directory the hour of ``ts`` is filed in, for ``source``."""
    year, month, day, _ = hour_of(ts)
    return source_dir(root, source) / f"{year:04d}" / f"{month:02d}" / f"{day:02d}"


def hour_start(ts: float) -> float:
    """The Unix time the hour of ``ts`` began."""
    return float(ts) - (float(ts) % 3600.0)


def files_for(root: Path, source: str, start: float, end: float) -> list[Path]:
    """Every file holding an hour that overlaps ``[start, end)``, oldest hour first.

    Found by walking the directories that exist rather than every hour in the
    window, so a sparse source over a long window costs what it holds.
    """
    base = source_dir(root, source)
    if not base.is_dir() or end <= start:
        return []
    first = hour_of(hour_start(start))
    last = hour_of(end - 1e-9) if end > start else first
    found: list[tuple[tuple[int, int, int, int], int, Path]] = []
    for year_dir in _numbered(base):
        year = int(year_dir.name)
        if not first[0] <= year <= last[0]:
            continue
        for month_dir in _numbered(year_dir):
            month = int(month_dir.name)
            if not first[:2] <= (year, month) <= last[:2]:
                continue
            for day_dir in _numbered(month_dir):
                day = int(day_dir.name)
                if not first[:3] <= (year, month, day) <= last[:3]:
                    continue
                for path in day_dir.iterdir():
                    match = _PART.match(path.name)
                    if match is None or not path.is_file():
                        continue
                    hour = (year, month, day, int(match.group("hour")))
                    if first <= hour <= last:
                        found.append((hour, int(match.group("part") or 0), path))
    return [path for _, _, path in sorted(found)]


def _numbered(directory: Path) -> list[Path]:
    """The subdirectories of ``directory`` whose names are numbers, in order."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_dir() and p.name.isdigit())


def event_time_of(payload: Any, *, arrived: float) -> tuple[float, str]:
    """When ``payload`` says it happened, and where that came from.

    The event time as `wactorz.core.labels.event_time` reads it (``ts``, then
    ``timestamp``), marked `FROM_PAYLOAD`; or ``arrived`` marked `FROM_ARRIVAL`
    when the payload says nothing usable, as ``docs/design/event-time.md``
    asks.
    """
    sentinel = -1.0
    said = event_time(payload, default=sentinel)
    if said == sentinel:
        return float(arrived), FROM_ARRIVAL
    return said, FROM_PAYLOAD
