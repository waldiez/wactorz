"""Judging a model's answers against the labels, the way its version says it is judged.

A shadow trial keeps, for each input, what the active and the shadow version
answered (`wactorz.core.shadow_trial`). Whether either was right is a question
for the labels: what a person, or an agent, said was true of the input's
source at the input's event time. The version's declaration
(`wactorz.core.judgement`) says which labels count and how an answer is read;
`judge` gives each side the figures that decision needs, with how sure they
are, and says whether the two sides really differ.

Each input is held against the labels of its own source unless the
declaration names one, so a model serving several devices is judged device by
device. An input no label covers is not judged, and is counted as such.

Each kind is one function here, chosen by the declaration's ``kind``; a kind
added later is one more function, and nothing already stored changes.

**A detector** flags an input by publishing something for it. It is judged
three ways, because each answers a different question:

- per input: precision, recall, F1, the false-positive rate, specificity and
  the Matthews correlation, which stays honest when faults are rare;
- per fault: a fault is a stretch a positive label holds, and it is caught when
  anything inside it was flagged; how long after its start the first flag came
  is the detection delay. A detector need not flag every reading of a long
  fault, only catch it early;
- per day: the alarms raised where no known fault was, which is what an
  operator lives with.

Every rate comes with a 95% Wilson interval, so a recall of 1.0 from three
faults reads as the uncertain thing it is. McNemar's test, over the inputs
exactly one side judged rightly, says whether the shadow is better than the
active version or the difference could be chance.
"""

import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .judgement import DETECTOR, Judgement
from .labels import Interval

#: The two sides a trial compares.
SIDES = ("active", "shadow")

#: The z of a two-sided 95% interval.
_Z95 = 1.959963984540054

#: Below this many discordant inputs McNemar's test is exact; above it, the
#: chi-square approximation is as good and costs nothing.
_EXACT_UP_TO = 500

#: The p below which a difference between the sides is called real.
SIGNIFICANT = 0.05

#: The shortest stretch of event time a rate per day is given for: over less,
#: one alarm would read as dozens a day.
_SHORTEST_DAY_FRACTION = 1 / 24

_DAY_S = 86400.0


# ── Statistics ────────────────────────────────────────────────────────────────


def ratio(part: int, whole: int) -> float | None:
    """``part / whole``, or None when there is nothing to divide."""
    return part / whole if whole else None


def wilson(successes: int, trials: int) -> list[float] | None:
    """The 95% Wilson score interval of a proportion, as ``[low, high]``; None with no trials."""
    if trials <= 0:
        return None
    p = successes / trials
    z2 = _Z95 * _Z95
    centre = (p + z2 / (2 * trials)) / (1 + z2 / trials)
    half = (_Z95 / (1 + z2 / trials)) * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials))
    return [max(0.0, centre - half), min(1.0, centre + half)]


def f1(tp: int, fp: int, fn: int) -> float | None:
    """The harmonic mean of precision and recall, from the counts: 0.0 when nothing was caught, None with nothing to count."""
    return ratio(2 * tp, 2 * tp + fp + fn)


def mcc(tp: int, fp: int, fn: int, tn: int) -> float | None:
    """The Matthews correlation of a 2x2 table; None when a row or a column is empty."""
    denominator = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
    if denominator == 0:
        return None
    return (tp * tn - fp * fn) / math.sqrt(denominator)


def mcnemar(active_only: int, shadow_only: int) -> dict[str, Any]:
    """Whether the sides differ, from the inputs only one of them judged rightly.

    Exact (two-sided binomial) for few such inputs, the chi-square with
    continuity correction for many. ``better`` names the side that was right
    more often when ``p`` is below `SIGNIFICANT`, and is None otherwise.
    """
    n = active_only + shadow_only
    if n == 0:
        p = 1.0
    elif n <= _EXACT_UP_TO:
        tail = sum(math.comb(n, k) for k in range(min(active_only, shadow_only) + 1))
        p = min(1.0, 2 * tail / 2**n)
    else:
        statistic = (abs(active_only - shadow_only) - 1) ** 2 / n
        p = math.erfc(math.sqrt(statistic / 2))
    better = None
    if p < SIGNIFICANT and active_only != shadow_only:
        better = "shadow" if shadow_only > active_only else "active"
    return {"active_only": active_only, "shadow_only": shadow_only, "p": p, "better": better}


# ── Reading the pairs ─────────────────────────────────────────────────────────


def label_at(intervals: Sequence[Interval], ts: float) -> str | None:
    """The label value holding at ``ts`` among ``intervals``, which do not overlap; None when none does."""
    for interval in intervals:
        if interval.start <= ts and (interval.end is None or ts < interval.end):
            return interval.label.value
    return None


def flagged(row: Mapping[str, Any], side: str) -> bool:
    """Whether ``side`` published something for the input of ``row``."""
    return row.get(f"{side}_output") not in (None, "")


def source_of(row: Mapping[str, Any], judgement: Judgement) -> str:
    """The label source ``row``'s input is judged against."""
    return judgement.source or str(row.get("source") or "")


def sources_to_read(
    rows: Iterable[Mapping[str, Any]], judgement: Judgement
) -> dict[str, tuple[float, float]]:
    """Each label source the rows are judged against, with the event times to read its labels over."""
    spans: dict[str, tuple[float, float]] = {}
    for row in rows:
        source = source_of(row, judgement)
        if not source:
            continue
        ts = float(row["ts"])
        low, high = spans.get(source, (ts, ts))
        spans[source] = (min(low, ts), max(high, ts))
    return {source: (low, high + 1.0) for source, (low, high) in spans.items()}


def labels_for(
    rows: Iterable[Mapping[str, Any]],
    judgement: Judgement,
    effective: Callable[..., Sequence[Interval]],
) -> dict[str, Sequence[Interval]]:
    """The labels each source the rows are judged against holds over their event times.

    ``effective`` is `Labels.effective`, or a stand-in with its signature.
    """
    return {
        source: effective(source, since=since, until=until)
        for source, (since, until) in sources_to_read(rows, judgement).items()
    }


# ── The detector ──────────────────────────────────────────────────────────────


def _rates(tp: int, fp: int, fn: int, tn: int) -> dict[str, Any]:
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1(tp, fp, fn),
        "false_positive_rate": ratio(fp, fp + tn),
        "specificity": ratio(tn, fp + tn),
        "mcc": mcc(tp, fp, fn, tn),
        "intervals": {
            "precision": wilson(tp, tp + fp),
            "recall": wilson(tp, tp + fn),
            "false_positive_rate": wilson(fp, fp + tn),
        },
    }


def _faults(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Sequence[Interval]],
    judgement: Judgement,
    positive: set[str],
) -> dict[str, dict[str, Any]]:
    """Per side: the faults the inputs saw, how many were caught, and how soon."""
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(source_of(row, judgement), []).append(row)
    seen = 0
    caught = dict.fromkeys(SIDES, 0)
    delays: dict[str, list[float]] = {side: [] for side in SIDES}
    for source, kept in by_source.items():
        for interval in labels.get(source, ()):
            if interval.label.value not in positive:
                continue
            inside = [
                r
                for r in kept
                if interval.start <= float(r["ts"])
                and (interval.end is None or float(r["ts"]) < interval.end)
            ]
            if not inside:
                continue
            seen += 1
            for side in SIDES:
                first = min((float(r["ts"]) for r in inside if flagged(r, side)), default=None)
                if first is not None:
                    caught[side] += 1
                    delays[side].append(max(0.0, first - interval.start))
    return {
        side: {
            "seen": seen,
            "caught": caught[side],
            "recall": ratio(caught[side], seen),
            "recall_interval": wilson(caught[side], seen),
            "delay_s": {
                "mean": statistics.fmean(delays[side]) if delays[side] else None,
                "median": statistics.median(delays[side]) if delays[side] else None,
                "max": max(delays[side]) if delays[side] else None,
            },
        }
        for side in SIDES
    }


def judge_detector(
    rows: Sequence[Mapping[str, Any]],
    judgement: Judgement,
    labels: Mapping[str, Sequence[Interval]],
) -> dict[str, Any]:
    """A detector's figures on each side, from the pairs ``rows`` and the labels of their sources."""
    positive = set(judgement.positive)
    counts = {side: [0, 0, 0, 0] for side in SIDES}  # tp, fp, fn, tn
    alarms = dict.fromkeys(SIDES, 0)
    right_only = dict.fromkeys(SIDES, 0)
    labelled = positives = unlabelled = 0
    judged_sources: set[str] = set()
    for row in rows:
        source = source_of(row, judgement)
        value = label_at(labels.get(source, ()), float(row["ts"]))
        is_positive = value in positive
        said = {side: flagged(row, side) for side in SIDES}
        for side in SIDES:
            alarms[side] += int(said[side] and not is_positive)
        if value is None:
            unlabelled += 1
            continue
        labelled += 1
        positives += int(is_positive)
        judged_sources.add(source)
        right = {side: said[side] == is_positive for side in SIDES}
        for side in SIDES:
            tp, fp, fn, tn = counts[side]
            counts[side] = [
                tp + int(said[side] and is_positive),
                fp + int(said[side] and not is_positive),
                fn + int(not said[side] and is_positive),
                tn + int(not said[side] and not is_positive),
            ]
        if right["active"] != right["shadow"]:
            right_only["active" if right["active"] else "shadow"] += 1
    times = [float(row["ts"]) for row in rows]
    days = (max(times) - min(times)) / _DAY_S if times else 0.0
    per_day = days >= _SHORTEST_DAY_FRACTION
    faults = _faults(rows, labels, judgement, positive)
    sides: dict[str, Any] = {}
    for side in SIDES:
        sides[side] = {
            **_rates(*counts[side]),
            "alarms_without_fault": alarms[side],
            "alarms_per_day": (alarms[side] / days) if per_day else None,
            "faults": faults[side],
        }
    return {
        "labelled": labelled,
        "positives": positives,
        "unlabelled": unlabelled,
        "sources": sorted(judged_sources),
        "days": days,
        **sides,
        "mcnemar": mcnemar(right_only["active"], right_only["shadow"]),
    }


#: Each kind's judge, by the kind a declaration names.
JUDGES: dict[
    str,
    Callable[
        [Sequence[Mapping[str, Any]], Judgement, Mapping[str, Sequence[Interval]]], dict[str, Any]
    ],
] = {
    DETECTOR: judge_detector,
}


def judge(
    rows: Iterable[Mapping[str, Any]],
    judgement: Judgement,
    labels: Mapping[str, Sequence[Interval]],
) -> dict[str, Any]:
    """The pairs ``rows`` judged as ``judgement`` says, against ``labels`` by label source.

    The answer names the kind and the declaration it was judged on, so a
    reader knows what the figures mean.
    """
    kept = list(rows)
    figures = JUDGES[judgement.kind](kept, judgement, labels)
    return {
        "kind": judgement.kind,
        "judged_on": judgement.to_json(),
        "positive": list(judgement.positive),
        **figures,
    }
