"""Fit a small anomaly model on synthetic normal motion and save it.

Stands in for whatever training produced your model. What `agent.py` needs is
a file it can load and an object with a `score(reading) -> float` method; swap
this for your own as long as that holds.
"""

import pickle

import numpy as np
from model import MODEL_PATH, MahalanobisModel


def main() -> None:
    rng = np.random.default_rng(seed=7)
    # Normal motion: small accelerations around gravity on the z axis.
    normal = rng.normal(loc=(0.0, 0.0, 1.0), scale=(0.3, 0.3, 0.2), size=(2000, 3))
    model = MahalanobisModel.fit(normal)
    MODEL_PATH.write_bytes(pickle.dumps(model))
    print(f"wrote {MODEL_PATH}")


if __name__ == "__main__":
    main()
