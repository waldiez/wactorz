# SPDX-License-Identifier: Apache-2.0
r"""The model as a recipe the train step runs: fit on a window of recorded readings, score on the rest.

What `train.py` does by hand on the healthy week, from what the recorder
filed: the `air-trainer` agent (`loop.py`), or `wactorz-train`, reads a
window of the readings source, hands it here as a frame, stores what `train`
returns as the next version of the model, and sets it as the shadow when
`score` says it is no worse than the version served
(``docs/design/train-step.md``).

    wactorz-train run air_quality.recipe --model air-quality --source sensors/air \
        --metric false_alarms --lower-is-better --state-dir state

The frame carries a `label` column when the version compared against says
what it is judged on, or the run asks for it: readings a person called a
fault are left out of the fit and are what `caught` is measured on. Without
labels every reading is taken as normal, and `false_alarms` is the share the
model would flag. The threshold travels inside the model (`k_sigma`, from the
run's settings), so a version and the threshold it was meant for stay
together.
"""

from collections.abc import Mapping
from typing import Any

import numpy as np

from .model import K_SIGMA, QUANTITIES, fit
from .model import score as score_reading
from .plant import ROOMS

#: The label values that say a reading was not normal; ``settings["positive"]`` names others.
POSITIVE = ("window_open", "ventilation_fault", "sensor_fault")


def _rows(frame: Any) -> list[dict[str, Any]]:
    """The frame's readings of the rooms the model knows, each with every quantity a number."""
    kept: list[dict[str, Any]] = []
    for row in frame.to_pylist():
        if row.get("device") not in ROOMS or not isinstance(row.get("ts"), (int, float)):
            continue
        if all(isinstance(row.get(q), (int, float)) for q in QUANTITIES):
            kept.append(row)
    return kept


def _positive(row: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
    """Whether a label calls the reading not normal; never without a label."""
    values = settings.get("positive", POSITIVE)
    positive = {values} if isinstance(values, str) else set(values)
    return row.get("label") in positive


def threshold_of(model: Mapping[str, Any]) -> float:
    """The threshold the model carries, else the package's default."""
    carried = model.get("k_sigma")
    return float(carried) if carried is not None else K_SIGMA


def train(frame: Any, settings: Mapping[str, Any]) -> dict[str, np.ndarray]:
    """The model of the usual: every reading of the window no label calls a fault, with its threshold."""
    model = fit(row for row in _rows(frame) if not _positive(row, settings))
    model["k_sigma"] = np.asarray(float(settings.get("k_sigma", K_SIGMA)))
    return model


def score(model: Mapping[str, Any], frame: Any) -> dict[str, float]:
    """The share of normal readings flagged, and of labelled faults caught where there are any."""
    rows = _rows(frame)
    k_sigma = threshold_of(model)
    flagged = np.array([score_reading(model, row)[0] >= k_sigma for row in rows], dtype=bool)
    positive = np.array([_positive(row, {}) for row in rows], dtype=bool)
    figures: dict[str, float] = {
        "false_alarms": float(flagged[~positive].mean()) if (~positive).any() else 0.0,
        "labelled_faults": float(positive.sum()),
    }
    if positive.any():
        figures["caught"] = float(flagged[positive].mean())
    return figures
