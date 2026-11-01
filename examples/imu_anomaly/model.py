"""The model: the mean of normal motion and the inverse of its covariance.

Two named arrays, saved as an ``.npz`` and kept on main with
``wactorz-artifacts`` as `MODEL_NAME`. No class and no pickle: loading it runs
nothing from the file, it loads in any process that has numpy, and a node an
agent moves to fetches it by name rather than needing this module beside it.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

#: The name the model is stored under, and that the agent names it by.
MODEL_NAME = "imu-detector"
#: Where `train.py` writes the file before storing it.
MODEL_FILE = Path(__file__).with_name("imu_model.npz")
#: The state directory the example's scripts run with, beside them rather than
#: wherever they are started from, so the model `train.py` stores is the one
#: `run.py` finds.
STATE_DIR = Path(__file__).with_name("state")
AXES = ("ax", "ay", "az")
#: Readings this far from normal, in units of its spread, are anomalies.
THRESHOLD = 4.0


def fit(samples: np.ndarray) -> dict[str, np.ndarray]:
    """The model of the motion in ``samples``, one reading per row."""
    mean = samples.mean(axis=0)
    covariance = np.cov(samples, rowvar=False) + np.eye(samples.shape[1]) * 1e-6
    return {"mean": mean, "inverse_covariance": np.linalg.inv(covariance)}


def score(model: Mapping[str, np.ndarray], reading: Mapping[str, Any]) -> float:
    """How far ``reading`` is from normal motion, in units of its spread."""
    x = np.array([float(reading.get(axis, 0.0)) for axis in AXES]) - model["mean"]
    return float(np.sqrt(x @ model["inverse_covariance"] @ x))
