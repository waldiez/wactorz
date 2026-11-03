# SPDX-License-Identifier: Apache-2.0
"""What a shadow trial showed: the comparison over its pairs, and its outcome in the graph.

A trial is the stretch a model's ``shadow`` pointer stood on one version. Its
pairs (`wactorz.agents.shadow_serving`) say what active and shadow answered to
each input and how long each took. `compare` reduces them to what a decision
needs: how often the two agreed, how far their scores sat apart, their
latencies, and, over the inputs a label covers, each side judged against the
labels as the version declares (`wactorz.core.judges`). `record_outcome` writes
the result as an ``outcome`` node, so the trial is part of the model's record
whichever way it ended.

Nothing here reads a file or a database beyond the graph it is given: the
pairs arrive as rows, from `wactorz.data.parquet.arrived_in` (the pairs that
arrived while the trial ran, whatever their event time) or a test.
"""

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .artifacts import ArtifactError, ArtifactStore
from .judgement import Judgement
from .judges import judge
from .labels import Interval
from .pointer_moves import source_of
from .provenance import OUTCOME, Provenance, ProvNode, stable_id

#: What the outcome is about, in its payload's ``what``.
SHADOW_TRIAL = "shadow_trial"

#: How a trial ended, in the outcome's ``ended``.
PROMOTED = "promoted"
CLEARED = "cleared"

#: The key a side's output is read for a score under.
SCORE_KEY = "score"


def _parsed(text: Any) -> Any:
    """A side's output as the function returned it, from the JSON the pair holds."""
    if text is None or text == "":
        return None
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _score(output: Any) -> float | None:
    """The numeric ``score`` an output carries, or None."""
    if isinstance(output, Mapping):
        value = output.get(SCORE_KEY)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _latency(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "mean_ms": (sum(values) / len(values)) if values else None,
        "p95_ms": _percentile(values, 0.95),
    }


def compare(
    rows: Iterable[Mapping[str, Any]],
    *,
    judgement: Judgement | None = None,
    labels: Mapping[str, Sequence[Interval]] | None = None,
) -> dict[str, Any]:
    """What the pairs ``rows`` show.

    ``agreement`` is the share of pairs where both sides answered alike:
    neither flagged, or both flagged with the same output. ``score`` is each
    side's mean ``score`` and the mean of shadow's minus active's, over the
    pairs where both gave one. With the ``judgement`` a version declares and
    the ``labels`` of the sources its inputs are judged against (from
    `Labels.effective`, by source), ``labels`` holds each side judged on them
    (`wactorz.core.judges`).
    """
    kept = list(rows)
    agreed = 0
    flagged = {"active": 0, "shadow": 0}
    latency: dict[str, list[float]] = {"active": [], "shadow": []}
    scores: dict[str, list[float]] = {"active": [], "shadow": []}
    differences: list[float] = []
    errors = 0
    versions: dict[str, set[int]] = {"active": set(), "shadow": set()}
    for row in kept:
        outputs = {side: _parsed(row.get(f"{side}_output")) for side in ("active", "shadow")}
        for side, output in outputs.items():
            flagged[side] += int(output is not None)
            ms = row.get(f"{side}_ms")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool):
                latency[side].append(float(ms))
            version = row.get(f"{side}_version")
            if isinstance(version, (int, float)) and not isinstance(version, bool):
                versions[side].add(int(version))
        if outputs["active"] == outputs["shadow"]:
            agreed += 1
        score = {side: _score(output) for side, output in outputs.items()}
        for side, value in score.items():
            if value is not None:
                scores[side].append(value)
        if score["active"] is not None and score["shadow"] is not None:
            differences.append(score["shadow"] - score["active"])
        if row.get("shadow_error"):
            errors += 1
    summary: dict[str, Any] = {
        "pairs": len(kept),
        "agreement": (agreed / len(kept)) if kept else None,
        "flagged": flagged,
        "shadow_errors": errors,
        "versions": {side: sorted(seen) for side, seen in versions.items()},
        "latency": {side: _latency(values) for side, values in latency.items()},
        "score": {
            "active_mean": (sum(scores["active"]) / len(scores["active"]))
            if scores["active"]
            else None,
            "shadow_mean": (sum(scores["shadow"]) / len(scores["shadow"]))
            if scores["shadow"]
            else None,
            "difference_mean": (sum(differences) / len(differences)) if differences else None,
            "scored": len(differences),
        },
    }
    if judgement is not None and labels is not None:
        summary["labels"] = judge(kept, judgement, labels)
    return summary


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _bucket(ts: float, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the pairs of one stretch of time show: each side's mean score and latency."""
    bucket: dict[str, Any] = {"ts": ts, "pairs": len(rows)}
    for side in ("active", "shadow"):
        scores = [_score(_parsed(row.get(f"{side}_output"))) for row in rows]
        latencies = [row.get(f"{side}_ms") for row in rows]
        bucket[f"{side}_score"] = _mean([s for s in scores if s is not None])
        bucket[f"{side}_ms"] = _mean(
            [float(ms) for ms in latencies if isinstance(ms, (int, float)) and ms is not True]
        )
    return bucket


def series(
    rows: Iterable[Mapping[str, Any]], *, since: float, until: float, points: int
) -> list[dict[str, Any]]:
    """The pairs ``rows`` over ``[since, until)``, in ``points`` stretches of equal length.

    Every stretch is there, with ``pairs`` 0 and no values where no input came,
    so that a chart shows a gap rather than a line across it. ``ts`` is the
    middle of its stretch.
    """
    if until <= since or points < 1:
        return []
    width = (until - since) / points
    stretches: list[list[Mapping[str, Any]]] = [[] for _ in range(points)]
    for row in rows:
        at = int((float(row["ts"]) - since) / width)
        if 0 <= at < points:
            stretches[at].append(row)
    return [_bucket(since + (at + 0.5) * width, kept) for at, kept in enumerate(stretches)]


def event_span(rows: Iterable[Mapping[str, Any]]) -> tuple[float, float] | None:
    """The event times ``rows`` cover, as ``[since, until)``; None for no rows.

    A trial is the pairs that arrived while it ran, whose event times may lie
    anywhere: labels are matched, and the trial drawn, over these instead.
    ``until`` lies just past the newest, so that it is inside.
    """
    times = [float(row["ts"]) for row in rows]
    if not times:
        return None
    return min(times), max(times) + 1.0


def judgement_of_trial(
    store: ArtifactStore,
    name: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    shadow: int | None = None,
) -> tuple[Judgement, str] | None:
    """What ``name``'s trial is judged on, and the version that said so; None when none says.

    The pairs say which versions answered them, so they decide: the shadow
    version they name most often, then the active one, each as it declares
    now. A window read after the trial, while another version is on trial, is
    judged as the versions that ran then. The pointers -- ``shadow`` as given,
    then where ``active`` stands -- are asked only when the pairs name no
    version that says. A version that is gone is passed over.
    """
    kept = list(rows)
    pointer = store.pointer(name, "active")
    ordered = [
        *_most_named(kept, "shadow_version"),
        *_most_named(kept, "active_version"),
        shadow,
        pointer.version if pointer else None,
    ]
    for version in dict.fromkeys(v for v in ordered if v is not None):
        try:
            judgement = store.resolve(f"{name}:{version}").judgement
        except ArtifactError:
            continue
        if judgement is not None:
            return judgement, f"{name}:{version}"
    return None


def _most_named(rows: Sequence[Mapping[str, Any]], column: str) -> list[int]:
    """The versions ``rows`` name in ``column``, the most named first, the newer first among equals."""
    named = Counter(int(r[column]) for r in rows if r.get(column) is not None)
    return sorted(named, key=lambda version: (-named[version], -version))


def outcome_id(name: str, shadow_version: int, since: float) -> str:
    """The id of the outcome of the trial that put ``shadow_version`` on trial at ``since``."""
    return stable_id("shadow-trial", name, shadow_version, since)


def record_outcome(
    prov: Provenance,
    *,
    name: str,
    shadow_version: int,
    active_version: int | None,
    since: float,
    until: float,
    ended: str,
    summary: Mapping[str, Any],
    by: str,
) -> ProvNode:
    """Write what the trial of ``name:shadow_version`` showed, as an ``outcome`` about the model.

    ``since`` is when the pointer was set, ``until`` when it was cleared or
    promoted, ``ended`` which of the two, ``by`` whoever did it. The id is the
    trial's, so an outcome written twice is one node. It stands alone until the
    train step writes ``model`` nodes for an ``evaluated_by`` edge to reach.
    """
    return prov.add_node(
        OUTCOME,
        ts=since,
        ts_end=until,
        source=source_of(name),
        actor=by,
        payload={
            "what": SHADOW_TRIAL,
            "model": name,
            "shadow_version": shadow_version,
            "active_version": active_version,
            "ended": ended,
            **dict(summary),
        },
        node_id=outcome_id(name, shadow_version, since),
    )


def outcomes_of(prov: Provenance, name: str) -> list[dict[str, Any]]:
    """Every trial outcome of ``name`` the graph holds, newest first."""
    nodes = [
        node
        for node in prov.nodes(OUTCOME, source=source_of(name), limit=None)
        if node.payload.get("what") == SHADOW_TRIAL
    ]
    newest_first = sorted(nodes, key=lambda node: (node.ts, node.seq), reverse=True)
    return [
        {"id": node.id, "since": node.ts, "until": node.ts_end, "by": node.actor, **node.payload}
        for node in newest_first
    ]
