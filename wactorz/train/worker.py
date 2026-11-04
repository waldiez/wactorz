# SPDX-License-Identifier: Apache-2.0
"""The child process a run fits in: ``python -m wactorz.train.worker <run-dir>``.

The parent (`wactorz.train.run`) writes ``spec.json`` into the run directory
and starts this in a fresh interpreter, with a timeout; this reads the window
through the loader, cuts the split, fits and scores each fold, scores the
version compared against on the same held-out rows, saves the model into the
run directory and writes ``result.json`` beside it, which says what was made
or why nothing was. What it prints goes to ``log.txt``, which the parent
keeps. Before anything heavy runs it seeds the random sources from the run's
seed and limits its own address space where the platform allows, so a recipe
that grows past the limit fails here and not in main. ``docs/design/
train-step.md`` is the decision.

Needs pyarrow, the ``data`` extra: the parent checks before starting it.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any

try:
    import resource  # not on Windows: the memory limit is then not applied
except ImportError:  # pragma: no cover - a platform without it
    resource = None

from ..core import models
from ..core.labels import Interval, Label
from ..data.frames import Fold, take, with_labels
from ..data.parquet import frame as read_frame
from .recipe import Recipe, load_recipe
from .spec import RESULT_FILE, SPEC_FILE, TrainError, TrainSpec
from .splits import folds_for

logger = logging.getLogger(__name__)


def limit_memory(megabytes: int) -> bool:
    """Cap this process's address space at ``megabytes``; whether the cap was applied.

    An address-space limit counts what a library reserves as well as what it
    uses, so a limit wants room above the data; zero applies none.
    """
    if megabytes <= 0 or resource is None:
        return False
    wanted = int(megabytes) * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            wanted = min(wanted, hard)
        resource.setrlimit(resource.RLIMIT_AS, (wanted, hard))
    except (ValueError, OSError):
        return False
    return True


def seed_everything(seed: int) -> list[str]:
    """Seed ``random``, numpy and torch where installed; the names seeded."""
    random.seed(seed)
    seeded = ["random"]
    try:
        import numpy as np  # optional dependency: numpy comes with the `ml` extra
    except ImportError:
        pass
    else:
        np.random.seed(seed)
        seeded.append("numpy")
    try:
        import torch  # pyright: ignore[reportMissingImports]  # optional dependency: the `ml` extra
    except ImportError:
        pass
    else:
        torch.manual_seed(seed)
        seeded.append("torch")
    return seeded


def intervals_from(given: list[dict[str, Any]], source: str) -> list[Interval]:
    """The labels the parent read, as the loader joins them."""
    found: list[Interval] = []
    for entry in given:
        end = entry.get("end")
        label = Label(
            id=str(entry.get("label_id") or ""),
            source=source,
            start=float(entry["start"]),
            end=None if end is None else float(end),
            value=str(entry.get("value") or ""),
            status="valid",
            recorded=0.0,
            actor=str(entry.get("actor") or ""),
        )
        found.append(Interval(label.start, label.end, label))
    return found


def mean_of(figures: list[dict[str, float]]) -> dict[str, float]:
    """The mean of each figure every fold reported."""
    if not figures:
        return {}
    keys = set(figures[0])
    for found in figures[1:]:
        keys &= set(found)
    return {key: sum(f[key] for f in figures) / len(figures) for key in sorted(keys)}


def fit_and_score(
    recipe: Recipe,
    frame: Any,
    folds: list[Fold],
    settings: dict[str, Any],
    parent: Any | None,
) -> tuple[Any, dict[str, Any]]:
    """Each fold fitted and scored, the parent scored beside it; the model to store and the figures.

    One fold stores the model it fitted; several store one fitted on every
    row, after the folds have said what such a model is worth.
    """
    scored: list[dict[str, Any]] = []
    model: Any = None
    started = time.monotonic()
    for fold in folds:
        model = recipe.fit(take(frame, fold.train), settings)
        test = take(frame, fold.test)
        entry: dict[str, Any] = {
            "rows": {"train": fold.to_dict()["rows"]["train"], "test": test.num_rows},
            "new": recipe.metrics(model, test),
        }
        if parent is not None:
            entry["parent"] = recipe.metrics(parent, test)
        scored.append(entry)
    if len(folds) > 1:
        model = recipe.fit(frame, settings)
    figures = {
        "new": mean_of([s["new"] for s in scored]),
        "parent": mean_of([s["parent"] for s in scored]) if parent is not None else None,
        "folds": scored,
        "train_seconds": time.monotonic() - started,
    }
    return model, figures


def work(run_dir: Path) -> dict[str, Any]:
    """Carry out the run ``run_dir`` describes; the result to write. Raises what went wrong."""
    given = json.loads((run_dir / SPEC_FILE).read_text(encoding="utf-8"))
    spec = TrainSpec.from_dict(given["run"])
    applied = limit_memory(spec.memory_mb)
    seeded = seed_everything(spec.seed)
    recipe = load_recipe(spec.recipe)
    frame = read_frame(Path(given["data_root"]), spec.source, spec.since, spec.until)
    if frame.num_rows == 0:
        raise TrainError.nothing_to_train_on(spec.source, spec.since, spec.until)
    labels = given.get("labels")
    if labels:
        frame = with_labels(frame, intervals_from(labels, spec.source))
    folds = folds_for(frame, spec.split)
    parent_entry = given.get("parent")
    parent = models.load(parent_entry) if parent_entry else None
    model, figures = fit_and_score(recipe, frame, folds, spec.settings, parent)
    path = recipe.write(model, run_dir)
    return {
        "ok": True,
        "model_path": str(path),
        "metrics": figures,
        "rows": {
            "window": frame.num_rows,
            "train": sum(f["rows"]["train"] for f in figures["folds"]),
            "test": sum(f["rows"]["test"] for f in figures["folds"]),
        },
        "split": {**spec.split.to_dict(), "folds": [f.to_dict() for f in folds]},
        "seed": spec.seed,
        "seeded": seeded,
        "memory_limit_applied": applied,
        "code_sha256": recipe.code_sha256,
        "recipe_path": str(recipe.path) if recipe.path else None,
        "labelled": bool(labels),
    }


def main(argv: list[str] | None = None) -> int:
    """Run, and write the result; 0 when a model was made."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        logger.error("usage: python -m wactorz.train.worker RUN_DIR")
        return 2
    run_dir = Path(args[0])
    try:
        result = work(run_dir)
    except Exception as exc:
        logger.exception("the run made nothing")
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    (run_dir / RESULT_FILE).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
