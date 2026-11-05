# SPDX-License-Identifier: Apache-2.0
"""The three tests drift is measured by: PSI, the two-sample KS test and ADWIN.

Each works on plain numbers and needs nothing beyond the standard library,
so a Pi install watches its models without installing a statistics package.
`psi` and `ks` compare two samples, a reference and a current one; `Adwin`
reads a sequence one value at a time and says when its mean changed. Which
windows they are run over, and what an alarm is, is `wactorz.drift.check`;
``docs/design/drift.md`` is the decision.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: How many bins PSI cuts the reference into, unless told otherwise.
DEFAULT_BINS = 10

#: The count added to every bin on both sides, so a bin one side has nothing
#: in gives a finite figure rather than a division by zero.
_SMOOTHING = 0.5

#: Where the Kolmogorov series is cut: the terms shrink as exp(-2 k² λ²), so
#: this many is more than enough for any λ that is not already one.
_KOLMOGOROV_TERMS = 100


def _numbers(values: Iterable[object]) -> list[float]:
    """The finite numbers among ``values``, in order; booleans and text are not numbers."""
    kept: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            kept.append(number)
    return kept


# ── PSI ───────────────────────────────────────────────────────────────────────


def edges_of(reference: Sequence[float], bins: int = DEFAULT_BINS) -> list[float]:
    """The cut points between ``bins`` bins of equal share of ``reference``: its quantiles, distinct."""
    ordered = sorted(reference)
    if len(ordered) < 2 or bins < 2:
        return []
    found: list[float] = []
    for i in range(1, bins):
        position = min(len(ordered) - 1, max(0, math.ceil(i * len(ordered) / bins) - 1))
        found.append(ordered[position])
    return list(dict.fromkeys(found))


def binned(values: Iterable[float], edges: Sequence[float]) -> list[int]:
    """How many of ``values`` fall in each bin ``edges`` cut: one more bin than edges."""
    counts = [0] * (len(edges) + 1)
    for value in values:
        counts[bisect_right(edges, value)] += 1
    return counts


def psi(
    reference: Iterable[object], current: Iterable[object], *, bins: int = DEFAULT_BINS
) -> float:
    """The population stability index of ``current`` against ``reference``.

    The reference is cut into ``bins`` bins of equal share, by its quantiles,
    and both samples are counted into them; the index is the sum over bins of
    ``(current share - reference share) * ln(current share / reference share)``.
    Under 0.1 nothing moved, 0.1 to 0.2 a moderate shift, over 0.2 a shift
    that matters. Each bin's count is smoothed by half a reading on both
    sides, so an empty bin gives a finite figure. Zero when either sample is
    empty, or the reference has one value.
    """
    base = _numbers(reference)
    now = _numbers(current)
    if not base or not now:
        return 0.0
    edges = edges_of(base, bins)
    if not edges:
        return 0.0
    expected = binned(base, edges)
    actual = binned(now, edges)
    k = len(expected)
    total = 0.0
    for e, a in zip(expected, actual, strict=True):
        share_e = (e + _SMOOTHING) / (len(base) + _SMOOTHING * k)
        share_a = (a + _SMOOTHING) / (len(now) + _SMOOTHING * k)
        total += (share_a - share_e) * math.log(share_a / share_e)
    return total


# ── KS ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KSResult:
    """The two-sample test: the largest distance between the distributions, and its asymptotic ``p``."""

    statistic: float
    p: float
    #: How many numbers each sample held.
    reference: int
    current: int


def kolmogorov_p(lambda_: float) -> float:
    """``P(K > λ)`` for the Kolmogorov distribution: ``2 Σ (-1)^(k-1) exp(-2 k² λ²)``."""
    if lambda_ <= 0:
        return 1.0
    total = 0.0
    for k in range(1, _KOLMOGOROV_TERMS + 1):
        term = math.exp(-2.0 * k * k * lambda_ * lambda_)
        if term < 1e-16:
            break
        total += term if k % 2 else -term
    return max(0.0, min(1.0, 2.0 * total))


def ks(reference: Iterable[object], current: Iterable[object]) -> KSResult:
    """The two-sample Kolmogorov–Smirnov test of ``current`` against ``reference``.

    ``statistic`` is ``D``, the largest distance between the two empirical
    distribution functions; ``p`` is the asymptotic probability of a ``D`` at
    least that large under one distribution, from the Kolmogorov series with
    ``λ = D √(nm / (n + m))``. ``D`` 0 and ``p`` 1 when either sample is empty.
    """
    base = sorted(_numbers(reference))
    now = sorted(_numbers(current))
    n, m = len(base), len(now)
    if not n or not m:
        return KSResult(0.0, 1.0, n, m)
    i = j = 0
    largest = 0.0
    while i < n and j < m:
        value = min(base[i], now[j])
        while i < n and base[i] <= value:
            i += 1
        while j < m and now[j] <= value:
            j += 1
        largest = max(largest, abs(i / n - j / m))
    lambda_ = largest * math.sqrt(n * m / (n + m))
    return KSResult(largest, kolmogorov_p(lambda_), n, m)


# ── ADWIN ─────────────────────────────────────────────────────────────────────

#: ADWIN's own constants, as published: the most buckets a row holds before
#: two are merged, how often the window is checked for a cut, the fewest
#: readings each side of a cut must hold, and how many the window needs
#: before any check.
MAX_BUCKETS = 5
CLOCK = 32
MIN_SIDE = 5
GRACE = 10

#: One bucket of the exponential histogram: the sum and the variance of the
#: readings it merged, ``2^row`` of them.
_Bucket = tuple[float, float]


class Adwin:
    """Adaptive windowing over a sequence: a window that shrinks when its mean has changed.

    Bifet and Gavaldà's ADWIN, with the exponential histogram that keeps the
    window in ``O(log n)`` buckets. `add` takes one value and returns whether
    the window was just cut, which is the detector saying that the readings
    before the cut and after it have different means, with confidence
    ``1 - delta``. `width` is then how many readings the window keeps: the
    ones since the change.

    ``delta`` trades false alarms for delay: smaller, fewer alarms and later.
    """

    def __init__(self, delta: float = 0.002) -> None:
        self.delta = delta
        #: Row ``i`` holds buckets of ``2^i`` readings, oldest first.
        self._rows: list[list[_Bucket]] = [[]]
        self.width = 0
        self.total = 0.0
        self._variance = 0.0
        self._ticks = 0
        #: How many cuts the window has had.
        self.changes = 0

    @property
    def mean(self) -> float | None:
        """The window's mean; None while it is empty."""
        return (self.total / self.width) if self.width else None

    @property
    def variance(self) -> float:
        """The window's variance."""
        return (self._variance / self.width) if self.width else 0.0

    def add(self, value: float) -> bool:
        """Take ``value`` as the newest reading; whether the window was cut because its mean changed."""
        self.width += 1
        if self.width > 1:
            earlier_mean = self.total / (self.width - 1)
            self._variance += (self.width - 1) * (value - earlier_mean) ** 2 / self.width
        self.total += value
        self._rows[0].append((value, 0.0))
        self._compress()
        return self._check()

    def _compress(self) -> None:
        """Merge the two oldest buckets of any row that holds one too many, into the next row."""
        for row in range(len(self._rows)):
            buckets = self._rows[row]
            if len(buckets) <= MAX_BUCKETS:
                break
            if row + 1 == len(self._rows):
                self._rows.append([])
            (total_a, var_a), (total_b, var_b) = buckets[0], buckets[1]
            size = 2**row
            mean_a, mean_b = total_a / size, total_b / size
            between = size * size * (mean_a - mean_b) ** 2 / (2 * size)
            self._rows[row + 1].append((total_a + total_b, var_a + var_b + between))
            del buckets[:2]

    def _drop_oldest(self) -> int:
        """Let the oldest bucket go, keeping the window's sum and variance right; how many readings it held."""
        row = len(self._rows) - 1
        while row > 0 and not self._rows[row]:
            self._rows.pop()
            row -= 1
        if not self._rows[row]:
            return 0
        total, variance = self._rows[row].pop(0)
        size = 2**row
        self.width -= size
        self.total -= total
        if self.width > 0:
            bucket_mean = total / size
            between = size * self.width * (bucket_mean - self.total / self.width) ** 2
            self._variance -= variance + between / (size + self.width)
        else:
            self._variance = 0.0
        if row > 0 and not self._rows[row]:
            self._rows.pop()
        return size

    def _check(self) -> bool:
        """Every `CLOCK` readings, look for a cut between the older and the newer part of the window."""
        self._ticks += 1
        if self._ticks % CLOCK or self.width <= GRACE:
            return False
        changed = False
        while self._cut_once():
            changed = True
        if changed:
            self.changes += 1
        return changed

    def _cut_once(self) -> bool:
        """Drop the oldest bucket when some split of the window has two means that differ; whether it did."""
        n0, n1 = 0, self.width
        u0, u1 = 0.0, self.total
        v0, v1 = 0.0, self._variance
        for row in range(len(self._rows) - 1, -1, -1):
            size = 2**row
            for index, (total, variance) in enumerate(self._rows[row]):
                if n0 > 0:
                    v0 += variance + n0 * size * (u0 / n0 - total / size) ** 2 / (n0 + size)
                if n1 > 0:
                    v1 -= variance + n1 * size * (u1 / n1 - total / size) ** 2 / (n1 + size)
                n0 += size
                n1 -= size
                u0 += total
                u1 -= total
                if row == 0 and index == len(self._rows[row]) - 1:
                    return False
                if n0 >= MIN_SIDE and n1 >= MIN_SIDE and self._differ(n0, n1, u0, u1):
                    self._drop_oldest()
                    return True
        return False

    def _differ(self, n0: int, n1: int, u0: float, u1: float) -> bool:
        """Whether the means of the two sides differ by more than the bound allows at ``delta``."""
        n = self.width
        dd = math.log(2 * math.log(n) / self.delta)
        v = self.variance
        m = 1.0 / (n0 - MIN_SIDE + 1) + 1.0 / (n1 - MIN_SIDE + 1)
        epsilon = math.sqrt(2 * m * v * dd) + 2.0 / 3.0 * dd * m
        return abs(u0 / n0 - u1 / n1) > epsilon


@dataclass(frozen=True)
class Change:
    """Where ADWIN cut a sequence: the position of the first reading after the change, and the means on each side."""

    index: int
    before: float | None
    after: float | None
    #: How many readings the window kept after the cut.
    kept: int


def adwin_changes(values: Sequence[object], *, delta: float = 0.002) -> list[Change]:
    """Every change ADWIN finds reading ``values`` in order, oldest first.

    A change's ``index`` is the first position the window kept, so the
    readings from it on have the new mean, ``after``, and those before it the
    old one, ``before``. A value that is not a number is skipped and the
    positions are of ``values`` as given.
    """
    detector = Adwin(delta)
    found: list[Change] = []
    numbers = [(i, number) for i, v in enumerate(values) for number in _numbers([v])]
    seen: list[float] = []
    for position, (_, number) in enumerate(numbers):
        seen.append(number)
        if detector.add(number):
            first_kept = position + 1 - detector.width
            earlier = seen[:first_kept]
            found.append(
                Change(
                    index=numbers[first_kept][0],
                    before=(sum(earlier) / len(earlier)) if earlier else None,
                    after=detector.mean,
                    kept=detector.width,
                )
            )
    return found
