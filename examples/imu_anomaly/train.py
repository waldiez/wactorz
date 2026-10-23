"""Fit the model on synthetic normal motion, save it, and store it on main.

Stands in for whatever training produced your model. What `agent.py` needs is a
model stored as `MODEL_NAME` that holds the arrays `model.score` reads; swap
this for your own training as long as that holds.

    python train.py                       # into this example's state directory
    python train.py --state-dir ../state  # into the state directory Wactorz runs with

Each run stores a new version; the agent loads the newest. The same, by hand:

    wactorz-artifacts add imu_model.npz imu-detector --state-dir state
"""

import argparse
import os
from pathlib import Path

import numpy as np
from model import MODEL_FILE, MODEL_NAME, STATE_DIR, fit

from wactorz.core.artifacts import Artifact, ArtifactStore


def train(state_dir: str | os.PathLike[str] = STATE_DIR) -> Artifact:
    """Fit, write `MODEL_FILE` and store it as the next version of `MODEL_NAME`."""
    rng = np.random.default_rng(seed=7)
    # Normal motion: small accelerations around gravity on the z axis.
    normal = rng.normal(loc=(0.0, 0.0, 1.0), scale=(0.3, 0.3, 0.2), size=(2000, 3))
    np.savez(MODEL_FILE, **fit(normal))
    store = ArtifactStore.in_state_dir(Path(state_dir))
    artifact = store.add(
        MODEL_FILE, MODEL_NAME, metadata={"source": "examples/imu_anomaly/train.py"}
    )
    print(f"wrote {MODEL_FILE}")
    print(f"stored {artifact.reference} in {store.root.resolve()}")
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--state-dir",
        default=STATE_DIR,
        type=Path,
        help="the state directory to store the model in (default: this example's)",
    )
    train(parser.parse_args(argv).state_dir)


if __name__ == "__main__":
    main()
