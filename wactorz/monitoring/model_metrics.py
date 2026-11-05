"""What the model loop reports to Prometheus: a trial's figures, drift, and the training runs.

Set where the figures are made: a trial's by main's check of it
(`wactorz.agents.main.shadow_serving`), drift's by the drift agent
(`wactorz.agents.drift_agent`), a run's by the trainer
(`wactorz.agents.train_agent`). A trial's and a model's drift series are
dropped when the trial ends or the agent stops, so a series that stops is a
trial that ended rather than a model that scores nothing.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, as the other modules here do. ``docs/design/drift.md`` says what
each is for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from prometheus_client import Counter, Gauge

SCORE = Gauge(
    "wactorz_model_score",
    "Each side of a model's shadow trial on the figure the policy compares, from the labels so far.",
    labelnames=("model", "side", "metric"),
    registry=None,
)
AGREEMENT = Gauge(
    "wactorz_model_shadow_agreement",
    "The share of a trial's pairs both sides answered alike.",
    labelnames=("model",),
    registry=None,
)
DRIFT = Gauge(
    "wactorz_model_drift",
    "A drift test's statistic on a column of a model's readings, at the last check.",
    labelnames=("model", "test", "column"),
    registry=None,
)
DRIFT_ALARM = Gauge(
    "wactorz_model_drift_alarm",
    "1 while a drift test on a column of a model's readings is over its threshold.",
    labelnames=("model", "test", "column"),
    registry=None,
)
TRAIN_SECONDS = Gauge(
    "wactorz_model_train_seconds",
    "How long the last training run of a model took, from its start to its result.",
    labelnames=("model",),
    registry=None,
)
TRAIN_RUNS = Counter(
    "wactorz_model_train_runs",
    "Training runs of a model, by how they ended.",
    labelnames=("model", "outcome"),
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (SCORE, AGREEMENT, DRIFT, DRIFT_ALARM, TRAIN_SECONDS, TRAIN_RUNS)

#: How a run ended, in `TRAIN_RUNS`'s ``outcome``.
OK = "ok"
FAILED = "failed"

#: The series each model holds, so they can be dropped exactly.
_trial_series: dict[str, set[tuple[str, str]]] = {}
_drift_series: dict[str, set[tuple[str, str]]] = {}


def _figure(side: Any, metric: str) -> float | None:
    if not isinstance(side, Mapping):
        return None
    value = side.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record_trial(name: str, summary: Mapping[str, Any], *, metric: str | None = None) -> None:
    """Set ``name``'s trial gauges from ``summary``, a trial compared (`wactorz.core.shadow_trial.compare`).

    ``metric`` is the figure the policy compares; unsaid, the main one of the
    kind the trial is judged as. A trial not judged sets the agreement alone.
    """
    agreement = summary.get("agreement")
    if isinstance(agreement, (int, float)) and not isinstance(agreement, bool):
        AGREEMENT.labels(model=name).set(float(agreement))
    labels = summary.get("labels")
    if not isinstance(labels, Mapping):
        return
    figure = metric or _main_metric(str(labels.get("kind") or ""))
    series = _trial_series.setdefault(name, set())
    for side in ("active", "shadow"):
        value = _figure(labels.get(side), figure)
        if value is None:
            continue
        SCORE.labels(model=name, side=side, metric=figure).set(value)
        series.add((side, figure))


def _main_metric(kind: str) -> str:
    # The policy's defaults, as `wactorz.core.promotion.METRICS_OF_KIND` names
    # them; kept here so this module needs nothing from core.
    return {"classifier": "macro_f1", "regressor": "mae"}.get(kind, "f1")


def forget_trial(name: str) -> None:
    """Drop ``name``'s trial series, when its trial ends."""
    _remove(AGREEMENT, (name,))
    for side, figure in _trial_series.pop(name, set()):
        _remove(SCORE, (name, side, figure))


def record_drift(name: str, readings: Iterable[Any]) -> None:
    """Set ``name``'s drift gauges from ``readings`` (`wactorz.drift.Reading`), one check's worth."""
    series = _drift_series.setdefault(name, set())
    for reading in readings:
        DRIFT.labels(model=name, test=reading.test, column=reading.column).set(
            float(reading.statistic)
        )
        DRIFT_ALARM.labels(model=name, test=reading.test, column=reading.column).set(
            1.0 if reading.alarmed else 0.0
        )
        series.add((reading.test, reading.column))


def forget_drift(name: str) -> None:
    """Drop ``name``'s drift series, when nothing watches it any more."""
    for test, column in _drift_series.pop(name, set()):
        _remove(DRIFT, (name, test, column))
        _remove(DRIFT_ALARM, (name, test, column))


def record_train(name: str, seconds: float, *, ok: bool) -> None:
    """Count one run of ``name`` and keep how long it took."""
    TRAIN_SECONDS.labels(model=name).set(float(seconds))
    TRAIN_RUNS.labels(model=name, outcome=OK if ok else FAILED).inc()


def _remove(metric: Gauge, labels: tuple[str, ...]) -> None:
    try:
        metric.remove(*labels)
    except KeyError:
        pass
