"""Fit the model on the building's healthy week and store it on main.

    python -m air_quality.train                       # v1, threshold 5, made active
    python -m air_quality.train --k-sigma 4 --set shadow   # a stricter v2, on trial beside it

Each run stores the next version of `MODEL_NAME`, with the threshold it is meant
for in its metadata, which is where the agent reads it from. It is stored in the
state directory Wactorz runs with (``WACTORZ_STATE_DIR``, else ``./state``), so
run this from where you start Wactorz, or pass ``--state-dir``.
"""

import argparse
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from wactorz.core.artifacts import Artifact, ArtifactStore
from wactorz.core.paths import resolve_state_dir

from .model import K_SIGMA, MODEL_NAME, fit
from .plant import DAY_S, SEED, WEEK, readings


def train(
    state_dir: Path, *, k_sigma: float = K_SIGMA, seed: int = SEED, pointer: str | None = "active"
) -> Artifact:
    """Fit on the healthy week, store it as the next version, and point ``pointer`` at it."""
    # Named arrays, as `np.savez` takes them.
    model: dict[str, Any] = fit(readings(WEEK, WEEK + 7 * DAY_S, seed=seed, faults=False))
    store = ArtifactStore.in_state_dir(state_dir)
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "air_quality.npz"
        np.savez(path, **model)
        artifact = store.add(
            path,
            MODEL_NAME,
            metadata={"k_sigma": k_sigma, "trained_on": "the healthy week of 2 March 2026"},
        )
    print(f"stored {artifact.reference} (k_sigma {k_sigma}) in {store.root.resolve()}")
    if pointer:
        store.move(pointer, artifact.reference, by="train", reason=f"k_sigma {k_sigma}")
        print(f"{pointer} is now {artifact.reference}")
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, default=None, help="default: Wactorz's own")
    parser.add_argument("--k-sigma", type=float, default=K_SIGMA, help="the alarm threshold")
    parser.add_argument("--set", choices=("active", "shadow"), default="active")
    args = parser.parse_args(argv)
    train(args.state_dir or Path(resolve_state_dir()), k_sigma=args.k_sigma, pointer=args.set)


if __name__ == "__main__":
    main()
