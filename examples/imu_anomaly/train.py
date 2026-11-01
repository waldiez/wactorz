"""Fit the model on synthetic normal motion, save it, and store it on main.

Stands in for whatever training produced your model. What `agent.py` needs is a
model stored as `MODEL_NAME` that holds the arrays `model.score` reads; swap
this for your own training as long as that holds.

    python train.py                       # into this example's state directory
    python train.py --state-dir ../state  # into the state directory Wactorz runs with
    python train.py --seed 1 --samples 10 --set shadow    # a version to try beside it

Each run stores a new version, with how it did on readings it was not fitted
on. The agent serves the version set `active` (the newest while none is), and
the dashboard's Models API moves the pointers too. The same, by hand:

    wactorz-artifacts add imu_model.npz imu-detector --state-dir state
"""

import argparse
import os
from pathlib import Path

import numpy as np
from model import AXES, MODEL_FILE, MODEL_NAME, STATE_DIR, THRESHOLD, fit, score

from wactorz.core.artifacts import Artifact, ArtifactStore

#: Normal motion: small accelerations around gravity on the z axis.
NORMAL_MEAN, NORMAL_SPREAD = (0.0, 0.0, 1.0), (0.3, 0.3, 0.2)
#: The held-out readings are drawn the same way for every version, so their
#: scores compare, from a stream of their own: a child stream, which no
#: ``--seed`` gives, so no training run is fitted on them.
HELD_OUT = np.random.SeedSequence(0, spawn_key=(1,))


def flagged(model: dict[str, np.ndarray], rows: np.ndarray) -> float:
    """The share of ``rows``, one reading each, that the agent would call abnormal."""
    return float(
        np.mean([score(model, dict(zip(AXES, row, strict=True))) >= THRESHOLD for row in rows])
    )


def held_out(model: dict[str, np.ndarray]) -> dict[str, float]:
    """How the model does on readings it was not fitted on: jolts caught, false alarms."""
    rng = np.random.default_rng(HELD_OUT)
    normal = rng.normal(NORMAL_MEAN, NORMAL_SPREAD, size=(1000, 3))
    jolts = rng.normal(NORMAL_MEAN, 1.0, size=(100, 3))
    return {
        "caught": round(flagged(model, jolts), 3),
        "false_alarms": round(flagged(model, normal), 4),
    }


def train(
    state_dir: str | os.PathLike[str] = STATE_DIR,
    *,
    seed: int = 7,
    samples: int = 2000,
    pointer: str | None = None,
) -> Artifact:
    """Fit, write `MODEL_FILE`, store it as the next version, and set ``pointer`` to it."""
    rng = np.random.default_rng(seed=seed)
    model = fit(rng.normal(NORMAL_MEAN, NORMAL_SPREAD, size=(samples, 3)))
    np.savez(MODEL_FILE, **model)
    store = ArtifactStore.in_state_dir(Path(state_dir))
    metadata = {"source": "examples/imu_anomaly/train.py", "seed": seed, "samples": samples}
    artifact = store.add(MODEL_FILE, MODEL_NAME, metadata=metadata | held_out(model))
    print(f"stored {artifact.reference} in {store.root.resolve()}: {artifact.metadata}")
    if pointer:
        store.move(pointer, artifact.reference, by="train.py", reason=f"seed {seed}")
        print(f"{pointer} is now {artifact.reference}")
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--state-dir",
        default=STATE_DIR,
        type=Path,
        help="the state directory to store the model in (default: this example's)",
    )
    parser.add_argument("--seed", type=int, default=7, help="what the readings are drawn from")
    parser.add_argument("--samples", type=int, default=2000, help="how many to fit on")
    parser.add_argument("--set", choices=("active", "shadow"), help="point this at the version")
    args = parser.parse_args(argv)
    train(args.state_dir, seed=args.seed, samples=args.samples, pointer=args.set)


if __name__ == "__main__":
    main()
