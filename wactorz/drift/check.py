# SPDX-License-Identifier: Apache-2.0
"""One check of a model's readings for drift: two windows, the tests over them, and what alarmed.

The agent (`wactorz.agents.drift_agent`) reads the reference and the current
window of the model's source through the loader and hands their rows here,
as the trial hands its pairs to `wactorz.core.shadow_trial`: nothing here
reads a file or a database, so the check is tested on lists and needs no
pyarrow. `check_features` runs PSI and KS on each numeric column;
`check_scores` holds a score column against the reference the same way and
runs ADWIN over the current window for the moment the scores changed. Each
test on each column is a `Reading`, alarmed or not. ``docs/design/drift.md``
is the decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .detectors import DEFAULT_BINS, adwin_changes, ks, psi

#: The tests, as a reading and an alarm name them.
PSI = "psi"
KS = "ks"
ADWIN = "adwin"
TESTS = (PSI, KS, ADWIN)

#: The columns every recorded row has that are not readings: the event time,
#: when it arrived and where its time came from (`wactorz.data.parquet`).
RESERVED_COLUMNS = frozenset({"ts", "arrived", "time_source"})

#: The column a score source holds its number in, unless said.
DEFAULT_SCORE_COLUMN = "score"


@dataclass(frozen=True)
class Thresholds:
    """When each test alarms, and what both windows need before any test runs."""

    #: PSI at or over this is a shift that matters.
    psi: float = 0.2
    #: KS's ``D`` at or over this, with ``p`` under ``ks_p``, is one.
    ks: float = 0.1
    ks_p: float = 0.05
    #: ADWIN's confidence: smaller, fewer alarms and later.
    adwin_delta: float = 0.002
    #: Rows each window must hold before a test is run over it.
    min_rows: int = 100
    #: Bins PSI cuts the reference into.
    bins: int = DEFAULT_BINS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Window:
    """A stretch of a source's event time, ``[since, until)``."""

    source: str
    since: float
    until: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Reading:
    """One test on one column: what it measured, and whether that is an alarm."""

    test: str
    column: str
    source: str
    statistic: float
    threshold: float
    alarmed: bool
    #: The test's probability, where it has one: KS.
    p: float | None = None
    #: The moment the readings changed, where the test finds one: ADWIN.
    at: float | None = None
    #: How many rows each side held.
    rows: dict[str, int] = field(default_factory=dict)
    #: What else the test says: ADWIN's means on either side of the cut.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """What an episode is keyed by: the test and the column."""
        return (self.test, self.column)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def words(self) -> str:
        """The reading as a line: ``psi on ax 0.31 (threshold 0.2)``."""
        line = f"{self.test} on {self.column} {self.statistic:.3g} (threshold {self.threshold:g}"
        if self.p is not None:
            line += f", p={self.p:.3g}"
        line += ")"
        if self.at is not None:
            line += f" from {self.at:.0f}"
        return line


def column_values(rows: Iterable[Mapping[str, Any]], column: str) -> list[float]:
    """The numbers ``column`` holds in ``rows``, in order; a row without one is skipped."""
    found: list[float] = []
    for row in rows:
        value = row.get(column)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found.append(float(value))
    return found


def numeric_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The columns of ``rows`` that hold numbers, in the order first seen, the reserved ones left out.

    A column counts when every value it holds is a number; one with text in
    it is not a feature.
    """
    seen: dict[str, bool] = {}
    for row in rows:
        for name, value in row.items():
            if name in RESERVED_COLUMNS or value is None:
                continue
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            seen[name] = seen.get(name, True) and numeric
    return [name for name, numeric in seen.items() if numeric]


def _compare(
    reference: Sequence[float],
    current: Sequence[float],
    *,
    column: str,
    source: str,
    thresholds: Thresholds,
) -> list[Reading]:
    """PSI and KS of ``current`` against ``reference``; nothing when either side is too short."""
    rows = {"reference": len(reference), "current": len(current)}
    if len(reference) < thresholds.min_rows or len(current) < thresholds.min_rows:
        return []
    index = psi(reference, current, bins=thresholds.bins)
    distance = ks(reference, current)
    return [
        Reading(
            PSI,
            column,
            source,
            statistic=index,
            threshold=thresholds.psi,
            alarmed=index >= thresholds.psi,
            rows=rows,
        ),
        Reading(
            KS,
            column,
            source,
            statistic=distance.statistic,
            threshold=thresholds.ks,
            p=distance.p,
            alarmed=distance.statistic >= thresholds.ks and distance.p < thresholds.ks_p,
            rows=rows,
        ),
    ]


def check_features(
    reference: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    *,
    source: str,
    columns: Sequence[str] | None = None,
    thresholds: Thresholds | None = None,
) -> list[Reading]:
    """PSI and KS of each feature column of ``current`` against ``reference``.

    ``columns`` names the features; unsaid, every numeric column of the
    current window. A column too short on either side gives no reading.
    """
    limits = thresholds or Thresholds()
    names = list(columns) if columns else numeric_columns(current)
    found: list[Reading] = []
    for column in names:
        found.extend(
            _compare(
                column_values(reference, column),
                column_values(current, column),
                column=column,
                source=source,
                thresholds=limits,
            )
        )
    return found


def check_scores(
    reference: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    *,
    source: str,
    column: str = DEFAULT_SCORE_COLUMN,
    thresholds: Thresholds | None = None,
) -> list[Reading]:
    """The score ``column`` against the reference by PSI and KS, and ADWIN over the current window.

    ADWIN reads the current window's scores in event time and reports the
    latest cut as the moment the scores changed, with the means on either
    side; no cut is a reading that did not alarm. The current window alone
    must hold ``min_rows`` scores for ADWIN to run.
    """
    limits = thresholds or Thresholds()
    before = column_values(reference, column)
    now = [
        (float(row["ts"]), float(row[column]))
        for row in current
        if isinstance(row.get(column), (int, float))
        and not isinstance(row.get(column), bool)
        and isinstance(row.get("ts"), (int, float))
    ]
    found = _compare(
        before, [value for _, value in now], column=column, source=source, thresholds=limits
    )
    if len(now) < limits.min_rows:
        return found
    changes = adwin_changes([value for _, value in now], delta=limits.adwin_delta)
    latest = changes[-1] if changes else None
    found.append(
        Reading(
            ADWIN,
            column,
            source,
            statistic=float(len(changes)),
            threshold=1.0,
            alarmed=latest is not None,
            at=now[latest.index][0] if latest is not None else None,
            rows={"reference": len(before), "current": len(now)},
            detail={
                "delta": limits.adwin_delta,
                "before": latest.before if latest is not None else None,
                "after": latest.after if latest is not None else None,
                "kept": latest.kept if latest is not None else None,
            },
        )
    )
    return found


def alarmed(readings: Iterable[Reading]) -> list[Reading]:
    """The readings that crossed their threshold."""
    return [reading for reading in readings if reading.alarmed]
