# SPDX-License-Identifier: Apache-2.0
"""Writing readings as Parquet files, and reading a window of them back as a frame.

`Writer` takes rows one at a time, keeps them in memory by the hour of their
event time, and writes each hour's rows as one file when the hour turns, when
the batch has grown, or when asked; a file is written beside its final name and
renamed into place, so it is whole or absent. `frame` reads every file whose
hour overlaps a window and gives the rows inside it, oldest first, as an Arrow
table with every column any of the files had. The layout is `files`; the
decision is ``docs/design/data-storage.md``.

Both block on the disk: from the event loop, go through ``asyncio.to_thread``.
pyarrow is the ``data`` extra; this module imports it, so import the module
where it is used, not at the top of something that must load without it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .files import DataError, files_for, hour_dir, hour_of, hour_start

logger = logging.getLogger(__name__)

#: How many rows a writer keeps before it writes a part of the hour out.
DEFAULT_BATCH_ROWS = 5000

#: The columns every file has, whatever the payloads carried.
TS = "ts"
ARRIVED = "arrived"
TIME_SOURCE = "time_source"

#: The schema a window with no files answers with.
EMPTY_SCHEMA = pa.schema([(TS, pa.float64()), (ARRIVED, pa.float64()), (TIME_SOURCE, pa.string())])


def normalise(row: Mapping[str, Any]) -> dict[str, Any]:
    """``row`` as a file holds it: numbers as floats, bools as bools, text as text, the rest as JSON.

    Every number is a float so that the same column is the same type in every
    file, whether a reading came as ``1`` or ``1.0``; a nested object or a list
    is kept as its JSON text, which a reader parses when it wants it. Raises
    `DataError` for a row without an event time.
    """
    if TS not in row:
        raise DataError.no_event_time(row)
    kept: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (bool, str)):
            kept[key] = value
        elif isinstance(value, (int, float)):
            kept[key] = float(value)
        elif isinstance(value, bytes):
            kept[key] = value
        else:
            kept[key] = json.dumps(value, default=str, separators=(",", ":"))
    kept[TS] = float(row[TS])
    return kept


class Writer:
    """Files the rows of one source under ``root``, an hour of event time per file."""

    def __init__(self, root: Path, source: str, *, batch_rows: int = DEFAULT_BATCH_ROWS) -> None:
        self.root = root
        self.source = source
        self.batch_rows = batch_rows
        #: Rows waiting to be written, by the start of the hour they fall in.
        self._pending: dict[float, list[dict[str, Any]]] = defaultdict(list)
        self._count = 0
        #: Where each row was written, for whoever asks what the last flush did.
        self.written: list[Path] = []

    @property
    def pending(self) -> int:
        """How many rows are waiting to be written."""
        return self._count

    def append(self, row: Mapping[str, Any]) -> list[Path]:
        """Keep ``row`` for its hour; write what is due, and say which files that made.

        Due is every hour but the newest once a row of a newer hour arrives,
        since nothing more of an older hour is expected, and the newest once it
        holds ``batch_rows`` rows.
        """
        kept = normalise(row)
        hour = hour_start(kept[TS])
        self._pending[hour].append(kept)
        self._count += 1
        newest = max(self._pending)
        due = [h for h in self._pending if h < newest]
        if len(self._pending[newest]) >= self.batch_rows:
            due.append(newest)
        return self._write(due) if due else []

    def flush(self) -> list[Path]:
        """Write every pending row out, whatever hour it is in; the files that made."""
        return self._write(list(self._pending))

    def close(self) -> list[Path]:
        """`flush`, for a writer that is done."""
        return self.flush()

    def _write(self, hours: Iterable[float]) -> list[Path]:
        made: list[Path] = []
        for hour in sorted(hours):
            rows = self._pending.pop(hour, [])
            if not rows:
                continue
            self._count -= len(rows)
            path = self._next_part(hour)
            write_rows(path, rows)
            made.append(path)
        self.written.extend(made)
        return made

    def _next_part(self, hour: float) -> Path:
        """The path the next file of ``hour`` is written to: the hour's own, else its next part."""
        directory = hour_dir(self.root, self.source, hour)
        _, _, _, hh = hour_of(hour)
        own = directory / f"{hh:02d}.parquet"
        if not own.exists():
            return own
        part = 1
        while (directory / f"{hh:02d}-{part:04d}.parquet").exists():
            part += 1
        return directory / f"{hh:02d}-{part:04d}.parquet"


def write_rows(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Write ``rows`` as one Parquet file at ``path``, whole or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([dict(row) for row in rows])
    temp = path.with_name(path.name + ".tmp")
    pq.write_table(table, temp, compression="zstd")
    temp.replace(path)


def frame(
    root: Path,
    source: str,
    start: float,
    end: float,
    *,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """The rows of ``source`` with event time in ``[start, end)``, oldest first.

    Every column any file in the window had is a column of the table, null
    where a file lacked it. ``columns`` keeps only those named, ``ts`` always
    among them. An empty table, with the three columns every file has, when
    the window holds nothing.
    """
    paths = files_for(root, source, start, end)
    wanted = None if columns is None else list(dict.fromkeys([TS, *columns]))
    tables = []
    for path in paths:
        try:
            table = pq.read_table(
                path,
                columns=_present(path, wanted),
                filters=[(TS, ">=", float(start)), (TS, "<", float(end))],
            )
        except (OSError, pa.ArrowException) as exc:
            logger.warning("[data] cannot read %s: %s", path, exc)
            continue
        tables.append(table)
    if not tables:
        return EMPTY_SCHEMA.empty_table()
    return pa.concat_tables(tables, promote_options="permissive").sort_by(TS)


def _present(path: Path, wanted: list[str] | None) -> list[str] | None:
    """Those of ``wanted`` the file at ``path`` has; None reads them all."""
    if wanted is None:
        return None
    names = set(pq.read_schema(path).names)
    return [name for name in wanted if name in names]
