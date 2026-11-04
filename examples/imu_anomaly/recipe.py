r"""The detector as a recipe the train step runs: fit on a window's normal readings, score on the rest.

What `train.py` does by hand on synthetic motion, from recorded readings:
the `trainer` agent, or `wactorz-train`, reads a window of `sensors/imu` the
recorder filed, hands it here as a frame, stores what `train` returns as the
next version of `imu-detector`, and sets it as the shadow when `score` says it
beats the version served (`docs/design/train-step.md`).

    wactorz-train run examples/imu_anomaly/recipe.py --model imu-detector \\
        --source sensors/imu --metric false_alarms --lower-is-better --state-dir state

The frame carries a `label` column when the model is judged, or the run asks
for it: readings an operator called a fault are left out of the fit and are
what `caught` is measured on. Without labels every reading is taken as
normal, and `false_alarms` is the share the model would flag.
"""

from collections.abc import Mapping
from typing import Any

import numpy as np
from model import AXES, THRESHOLD, fit
from model import score as score_reading

#: The label values that say a reading was not normal; ``settings["positive"]`` names others.
POSITIVE = ("fault", "real_problem")


def _rows(frame: Any) -> np.ndarray:
    """One reading per row, the axes as columns, a missing axis as zero."""
    columns = [
        [0.0 if v is None else float(v) for v in frame.column(axis).to_pylist()]
        if axis in frame.column_names
        else [0.0] * frame.num_rows
        for axis in AXES
    ]
    return np.column_stack(columns) if frame.num_rows else np.zeros((0, len(AXES)))


def _positive(frame: Any, settings: Mapping[str, Any]) -> np.ndarray:
    """Which rows a label calls not normal; none without a label column."""
    if "label" not in frame.column_names:
        return np.zeros(frame.num_rows, dtype=bool)
    values = settings.get("positive", POSITIVE)
    positive = {values} if isinstance(values, str) else set(values)
    return np.array([v in positive for v in frame.column("label").to_pylist()], dtype=bool)


def train(frame: Any, settings: Mapping[str, Any]) -> dict[str, np.ndarray]:
    """The model of normal motion: every reading of the window no label calls a fault."""
    return fit(_rows(frame)[~_positive(frame, settings)])


def score(model: Mapping[str, np.ndarray], frame: Any) -> dict[str, float]:
    """The share of normal readings flagged, and of labelled faults caught where there are any."""
    rows = _rows(frame)
    flagged = np.array(
        [score_reading(model, dict(zip(AXES, row, strict=True))) >= THRESHOLD for row in rows],
        dtype=bool,
    )
    positive = _positive(frame, {})
    figures: dict[str, float] = {
        "false_alarms": float(flagged[~positive].mean()) if (~positive).any() else 0.0,
        "labelled_faults": float(positive.sum()),
    }
    if positive.any():
        figures["caught"] = float(flagged[positive].mean())
    return figures
