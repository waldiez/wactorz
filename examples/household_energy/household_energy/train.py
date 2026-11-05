"""Fit the model on the house's noted week and store it on main.

    python -m household_energy.train                                  # v1: the watts alone, made active
    python -m household_energy.train --features watts,minutes --set shadow   # v2, on trial beside it

Each run stores the next version of `MODEL_NAME`, with the features it looks
at in the model and in the version's metadata. It is stored in the state
directory Wactorz runs with (``WACTORZ_STATE_DIR``, else ``./state``), so run
this from where you start Wactorz, or pass ``--state-dir``.
"""

import argparse
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from wactorz.core.artifacts import Artifact, ArtifactStore
from wactorz.core.paths import resolve_state_dir

from .model import FEATURES, FIRST_FEATURES, MODEL_NAME, find_runs, fit
from .plant import DAY_S, SEED, WEEK, readings, run_at


class UnknownFeatureError(ValueError):
    """A feature the model cannot look at."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no feature {name!r}: choose from {', '.join(FEATURES)}")


def noted_week(seed: int = SEED) -> list[dict[str, Any]]:
    """Every run of the noted week, as the agent finds it, with the appliance it was."""
    noted = []
    for run in find_runs(readings(WEEK, WEEK + 7 * DAY_S, seed)):
        truth = run_at(float(run["ts"]), seed)
        if truth is not None:
            noted.append({**run, "appliance": truth.appliance})
    return noted


def train(
    state_dir: Path,
    *,
    features: Sequence[str] = FIRST_FEATURES,
    seed: int = SEED,
    pointer: str | None = "active",
) -> Artifact:
    """Fit on the noted week, store it as the next version, and point ``pointer`` at it."""
    for name in features:
        if name not in FEATURES:
            raise UnknownFeatureError(name)
    # Named arrays, as `np.savez` takes them.
    model: dict[str, Any] = fit(noted_week(seed), features)
    store = ArtifactStore.in_state_dir(state_dir)
    looks_at = ", ".join(features)
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "appliances.npz"
        np.savez(path, **model)
        artifact = store.add(
            path,
            MODEL_NAME,
            metadata={"features": list(features), "trained_on": "the noted week of 2 March 2026"},
        )
    print(f"stored {artifact.reference} (looks at {looks_at}) in {store.root.resolve()}")
    if pointer:
        store.move(pointer, artifact.reference, by="train", reason=f"looks at {looks_at}")
        print(f"{pointer} is now {artifact.reference}")
    return artifact


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--state-dir", type=Path, default=None, help="default: Wactorz's own")
    parser.add_argument(
        "--features",
        default=",".join(FIRST_FEATURES),
        help=f"what the model looks at, from {','.join(FEATURES)}",
    )
    parser.add_argument("--set", choices=("active", "shadow"), default="active")
    args = parser.parse_args(argv)
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    try:
        train(args.state_dir or Path(resolve_state_dir()), features=features, pointer=args.set)
    except UnknownFeatureError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
