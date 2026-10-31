# SPDX-License-Identifier: Apache-2.0
"""What the model loop does with a frame: cut it, split it, label it, describe it.

A frame is what `wactorz.data.parquet.frame` returns: an Arrow table of one
source over a window, oldest first, with ``ts`` in event time. Everything here
works on row positions of that table, so a split is a few index ranges and a
manifest that says how it was made, which a trainer stores with the version
it produces and anyone can replay. No split shuffles rows: a reading's
neighbours in time are the signal, and a model scored on them has seen them.

Segments are the unit the splits group on: a stretch of rows that belongs
together, cut where event time jumps, along a label's intervals, where a
column changes, or every so many seconds. ``docs/design/data-storage.md`` is
the decision.

Nothing here reads a file: pyarrow is imported for the table type and the
export, and this module imports it, so import it where it is used.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..core.labels import Interval
from .files import DataError
from .parquet import TS

#: A stretch of row positions, ``[start, end)``.
Range = tuple[int, int]

#: How a frame is cut into segments.
BY_GAP = "gap"
BY_LABEL = "label"
BY_COLUMN = "column"
BY_LENGTH = "length"

#: The most folds `leave_k_out` makes without being told otherwise.
DEFAULT_MAX_FOLDS = 20

#: The quantiles a profile reports of a numeric column.
QUANTILES = (0.05, 0.5, 0.95)

#: How many of a text column's values a profile names, most common first.
TOP_VALUES = 5


class SplitError(DataError):
    """A split that cannot be made from what it was given."""

    @classmethod
    def nothing_to_split(cls) -> SplitError:
        return cls("a time split needs rows, and the frame has none")

    @classmethod
    def too_few_segments(cls, have: int, k: int) -> SplitError:
        return cls(f"leave-{k}-out needs more than {k} segments; there are {have}")

    @classmethod
    def too_few_folds(cls, rows: int, k: int) -> SplitError:
        return cls(f"{k} forward folds need at least {k + 1} rows; there are {rows}")

    @classmethod
    def unknown_cut(cls, by: str) -> SplitError:
        return cls(
            f"segments are cut by {BY_GAP!r}, {BY_LABEL!r}, {BY_COLUMN!r} or {BY_LENGTH!r}, not {by!r}"
        )

    @classmethod
    def needs(cls, by: str, what: str) -> SplitError:
        return cls(f"cutting by {by!r} needs {what}")

    @classmethod
    def no_column(cls, column: str) -> SplitError:
        return cls(f"the frame has no column {column!r}")


@dataclass(frozen=True)
class Segment:
    """Rows ``[start, end)`` that belong together, and why."""

    start: int
    end: int
    ts_start: float
    ts_end: float
    #: What put them together: the gap's number, the label's value, the
    #: column's value, or the span's number.
    key: str

    @property
    def rows(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Fold:
    """One way of holding rows out: what is trained on, what is scored on, and how it was chosen."""

    train: tuple[Range, ...]
    test: tuple[Range, ...]
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The fold as a manifest: JSON, stored with the version it trained."""
        return {
            "kind": self.kind,
            "params": dict(self.params),
            "train": [list(r) for r in self.train],
            "test": [list(r) for r in self.test],
            "rows": {"train": _count(self.train), "test": _count(self.test)},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fold:
        return cls(
            train=tuple((int(s), int(e)) for s, e in data.get("train", [])),
            test=tuple((int(s), int(e)) for s, e in data.get("test", [])),
            kind=str(data.get("kind", "")),
            params=dict(data.get("params", {})),
        )


def _count(ranges: Iterable[Range]) -> int:
    return sum(max(0, end - start) for start, end in ranges)


def _timestamps(frame: pa.Table) -> list[float]:
    return [float(value) for value in frame.column(TS).to_pylist()]


def take(frame: pa.Table, ranges: Iterable[Range]) -> pa.Table:
    """The rows of ``frame`` inside ``ranges``, in order: a fold's train or test set."""
    pieces = [frame.slice(start, end - start) for start, end in ranges if end > start]
    if not pieces:
        return frame.schema.empty_table()
    return pa.concat_tables(pieces)


# ── Segments ──────────────────────────────────────────────────────────────────


def segments(
    frame: pa.Table,
    by: str = BY_GAP,
    *,
    gap_s: float | None = None,
    intervals: Sequence[Interval] | None = None,
    column: str | None = None,
    length_s: float | None = None,
) -> list[Segment]:
    """Cut ``frame`` into the stretches that belong together.

    ``by="gap"`` cuts where event time jumps by more than ``gap_s``, which
    defaults to ten times the median interval between rows; ``"label"`` cuts
    along ``intervals`` from `Labels.effective`, one segment per interval that
    covers rows, rows no interval covers left out; ``"column"`` cuts where
    ``column`` changes value; ``"length"`` every ``length_s`` seconds of event
    time. Empty for an empty frame.
    """
    ts = _timestamps(frame)
    if not ts:
        return []
    if by == BY_GAP:
        return _by_gap(ts, gap_s)
    if by == BY_LABEL:
        if intervals is None:
            raise SplitError.needs(by, "intervals")
        return _by_label(ts, intervals)
    if by == BY_COLUMN:
        if not column:
            raise SplitError.needs(by, "a column")
        return _by_column(ts, frame.column(column).to_pylist())
    if by == BY_LENGTH:
        if not length_s or length_s <= 0:
            raise SplitError.needs(by, "a length in seconds")
        return _by_length(ts, length_s)
    raise SplitError.unknown_cut(by)


def _by_gap(ts: Sequence[float], gap_s: float | None) -> list[Segment]:
    if gap_s is None:
        steps = sorted(b - a for a, b in pairwise(ts) if b > a)
        gap_s = 10 * steps[len(steps) // 2] if steps else math.inf
    found: list[Segment] = []
    start = 0
    for index in range(1, len(ts) + 1):
        if index == len(ts) or ts[index] - ts[index - 1] > gap_s:
            found.append(Segment(start, index, ts[start], ts[index - 1], str(len(found))))
            start = index
    return found


def _by_label(ts: Sequence[float], intervals: Sequence[Interval]) -> list[Segment]:
    found: list[Segment] = []
    for interval in sorted(intervals, key=lambda i: i.start):
        inside = [
            index
            for index, moment in enumerate(ts)
            if interval.start <= moment and (interval.end is None or moment < interval.end)
        ]
        if not inside:
            continue
        # Rows are in time order, so what an interval covers is one stretch.
        start, end = inside[0], inside[-1] + 1
        found.append(Segment(start, end, ts[start], ts[end - 1], interval.label.value))
    return found


def _by_column(ts: Sequence[float], values: Sequence[Any]) -> list[Segment]:
    found: list[Segment] = []
    start = 0
    for index in range(1, len(ts) + 1):
        if index == len(ts) or values[index] != values[start]:
            found.append(Segment(start, index, ts[start], ts[index - 1], str(values[start])))
            start = index
    return found


def _by_length(ts: Sequence[float], length_s: float) -> list[Segment]:
    found: list[Segment] = []
    start = 0
    for index in range(1, len(ts) + 1):
        if index == len(ts) or ts[index] - ts[start] >= length_s:
            found.append(Segment(start, index, ts[start], ts[index - 1], str(len(found))))
            start = index
    return found


# ── Splits ────────────────────────────────────────────────────────────────────


def time_split(frame: pa.Table, at: float) -> Fold:
    """Before ``at`` to train on, from ``at`` to score on: the split a regime change asks for."""
    ts = _timestamps(frame)
    if not ts:
        raise SplitError.nothing_to_split()
    cut = next((index for index, moment in enumerate(ts) if moment >= at), len(ts))
    return Fold(
        train=((0, cut),),
        test=((cut, len(ts)),),
        kind="time_split",
        params={"at": float(at), "from": ts[0], "to": ts[-1]},
    )


def rolling_origin(frame: pa.Table, k: int, *, min_train: int = 1) -> list[Fold]:
    """``k`` forward folds: each trains on everything before its stretch and scores on the stretch.

    The rows after the first ``min_train`` are cut into ``k`` stretches of
    equal size; fold ``i`` scores on stretch ``i`` and trains on all rows
    before it, so no fold ever sees its own future.
    """
    rows = frame.num_rows
    if k < 1 or rows < min_train + k:
        raise SplitError.too_few_folds(rows, k)
    span = rows - min_train
    folds: list[Fold] = []
    for index in range(k):
        start = min_train + (span * index) // k
        end = min_train + (span * (index + 1)) // k
        folds.append(
            Fold(
                train=((0, start),),
                test=((start, end),),
                kind="rolling_origin",
                params={"k": k, "fold": index, "min_train": min_train},
            )
        )
    return folds


def leave_k_out(
    segs: Sequence[Segment],
    k: int = 1,
    *,
    seed: int | None = None,
    max_folds: int = DEFAULT_MAX_FOLDS,
) -> list[Fold]:
    """Every way of holding ``k`` segments out, or ``max_folds`` of them drawn with ``seed``.

    A fold scores on the segments held out and trains on the rest, each as
    the row ranges of its segments, so a segment is never cut. With more
    choices than ``max_folds`` a seeded sample is taken, and the seed is in
    the manifest so the same folds come back.
    """
    if k < 1 or len(segs) <= k:
        raise SplitError.too_few_segments(len(segs), k)
    choices = list(combinations(range(len(segs)), k))
    sampled = len(choices) > max_folds
    if sampled:
        # A seeded draw of folds, not a secret: the seed is in the manifest.
        choices = random.Random(seed).sample(choices, max_folds)  # noqa: S311
    folds: list[Fold] = []
    for held in choices:
        out = set(held)
        folds.append(
            Fold(
                train=tuple((s.start, s.end) for i, s in enumerate(segs) if i not in out),
                test=tuple((s.start, s.end) for i, s in enumerate(segs) if i in out),
                kind="leave_k_out",
                params={
                    "k": k,
                    "held_out": [segs[i].key for i in held],
                    "segments": len(segs),
                    "seed": seed if sampled else None,
                },
            )
        )
    return folds


# ── Labels ────────────────────────────────────────────────────────────────────


def with_labels(
    frame: pa.Table, intervals: Sequence[Interval], *, column: str = "label"
) -> pa.Table:
    """``frame`` with a ``column`` holding the label at each row's event time, null where none holds."""
    ordered = sorted(intervals, key=lambda i: i.start)
    values: list[str | None] = []
    for moment in _timestamps(frame):
        found = None
        for interval in ordered:
            if interval.start <= moment and (interval.end is None or moment < interval.end):
                found = interval.label.value
        values.append(found)
    if column in frame.column_names:
        frame = frame.drop_columns([column])
    return frame.append_column(column, pa.array(values, type=pa.string()))


# ── The dataset card ──────────────────────────────────────────────────────────


def profile(frame: pa.Table, *, source: str = "") -> dict[str, Any]:
    """What a window of ``source`` holds: its span, rate, gaps, and each column described.

    The card the data tab and ``wactorz-data profile`` show. ``rate_per_s`` is
    rows over the span; ``gaps`` counts the intervals over ten times the
    median, with the longest; a numeric column reports count, missing, min,
    max, mean and the quantiles in `QUANTILES`, a text column its distinct
    values and the most common.
    """
    ts = _timestamps(frame)
    rows = len(ts)
    card: dict[str, Any] = {"source": source, "rows": rows, "columns": {}}
    if not rows:
        return card
    span = ts[-1] - ts[0]
    steps = sorted(b - a for a, b in pairwise(ts))
    median = steps[len(steps) // 2] if steps else 0.0
    long = [s for s in steps if median and s > 10 * median]
    card.update(
        {
            "from": ts[0],
            "to": ts[-1],
            "span_s": span,
            "rate_per_s": (rows / span) if span > 0 else None,
            "median_step_s": median,
            "gaps": {"count": len(long), "longest_s": max(long) if long else 0.0},
        }
    )
    for name in frame.column_names:
        if name == TS:
            continue
        card["columns"][name] = _describe(frame.column(name).to_pylist())
    return card


def _describe(values: Sequence[Any]) -> dict[str, Any]:
    present = [v for v in values if v is not None]
    missing = len(values) - len(present)
    numbers = [float(v) for v in present if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if present and len(numbers) == len(present):
        ordered = sorted(numbers)
        return {
            "type": "number",
            "count": len(numbers),
            "missing": missing,
            "min": ordered[0],
            "max": ordered[-1],
            "mean": sum(ordered) / len(ordered),
            "quantiles": {
                str(q): ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]
                for q in QUANTILES
            },
        }
    counts: dict[str, int] = {}
    for value in present:
        key = value if isinstance(value, str) else json.dumps(value, default=str)
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_VALUES]
    return {
        "type": "bool" if present and all(isinstance(v, bool) for v in present) else "text",
        "count": len(present),
        "missing": missing,
        "distinct": len(counts),
        "top": [{"value": value, "count": count} for value, count in top],
    }


# ── A column over time ────────────────────────────────────────────────────────

#: How many buckets a series has when not told.
DEFAULT_POINTS = 200


def series(
    frame: pa.Table,
    column: str,
    *,
    since: float,
    until: float,
    points: int = DEFAULT_POINTS,
) -> dict[str, Any]:
    """``column`` over ``[since, until)`` in ``points`` buckets of equal width, for a chart.

    Each bucket says how many rows fell in it and, of the numbers among them,
    their mean, least and most; an empty bucket is a gap, with nulls. Rows
    whose value is not a number count and give nothing else. Raises
    `SplitError` for a column the frame lacks.
    """
    if column not in frame.column_names:
        raise SplitError.no_column(column)
    points = max(1, points)
    width = (until - since) / points
    counts = [0] * points
    sums = [0.0] * points
    least: list[float | None] = [None] * points
    most: list[float | None] = [None] * points
    numbers = [0] * points
    for ts, value in zip(_timestamps(frame), frame.column(column).to_pylist(), strict=True):
        if not since <= ts < until or width <= 0:
            continue
        index = min(points - 1, int((ts - since) / width))
        counts[index] += 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            numbers[index] += 1
            sums[index] += number
            low, high = least[index], most[index]
            least[index] = number if low is None or number < low else low
            most[index] = number if high is None or number > high else high
    return {
        "column": column,
        "since": since,
        "until": until,
        "points": points,
        "buckets": [
            {
                "from": since + i * width,
                "to": since + (i + 1) * width,
                "count": counts[i],
                "mean": (sums[i] / numbers[i]) if numbers[i] else None,
                "min": least[i],
                "max": most[i],
            }
            for i in range(points)
        ],
    }


# ── Export ────────────────────────────────────────────────────────────────────


def export(frame: pa.Table, path: str | Path) -> Path:
    """Write ``frame`` as one Parquet file at ``path``, for a notebook or a hand-off."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(frame, target, compression="zstd")
    return target


def segment_dicts(segs: Iterable[Segment]) -> list[dict[str, Any]]:
    """``segs`` as JSON, for a route or a manifest."""
    return [asdict(s) for s in segs]
