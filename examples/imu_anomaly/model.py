"""The model class, in a module of its own so a pickle of it can be loaded anywhere.

Pickle records where a class lives. A class defined in the script that trains
it is recorded as living in `__main__`, which is a different module in every
process that later loads the file; one that lives here is found by the agent,
by the training script and by anything else that imports it.
"""

from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).with_name("imu_model.pkl")
AXES = ("ax", "ay", "az")


class MahalanobisModel:
    """Distance from the mean of normal motion, in units of its spread."""

    def __init__(self, mean: np.ndarray, inverse_covariance: np.ndarray) -> None:
        self.mean = mean
        self.inverse_covariance = inverse_covariance

    @classmethod
    def fit(cls, samples: np.ndarray) -> "MahalanobisModel":
        mean = samples.mean(axis=0)
        covariance = np.cov(samples, rowvar=False) + np.eye(samples.shape[1]) * 1e-6
        return cls(mean, np.linalg.inv(covariance))

    def score(self, reading: dict) -> float:
        x = np.array([float(reading.get(axis, 0.0)) for axis in AXES]) - self.mean
        return float(np.sqrt(x @ self.inverse_covariance @ x))
