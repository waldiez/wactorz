# SPDX-License-Identifier: Apache-2.0
"""Loading the user's recipe: the module with ``train`` and ``score``.

A recipe is named by its importable name, ``my_models.recipe``, or by its
file, ``examples/imu_anomaly/recipe.py``, which may import the modules beside
it, as the IMU example's does. It defines ``train(frame, settings) -> model`` and
``score(model, frame) -> metrics``, and may define ``save(model, directory) ->
path``; without one, a mapping of numpy arrays is saved as ``.npz``. The
source's hash is kept with every version the recipe makes, so a version can
be traced to the code as it was. ``docs/design/train-step.md`` is the decision.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .spec import TrainError

#: The file the default ``save`` writes, in the run's directory.
DEFAULT_MODEL_FILE = "model.npz"


class NoLoaderError(ImportError):
    """A file Python has no loader for."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Python has no loader for {name}")


@dataclass(frozen=True)
class Recipe:
    """A recipe as loaded: its functions, and the source they came from."""

    name: str
    path: Path | None
    code_sha256: str
    train: Callable[..., Any]
    score: Callable[..., Any]
    save: Callable[..., Any] | None

    def fit(self, frame: Any, settings: Mapping[str, Any]) -> Any:
        """``train`` on ``frame`` with ``settings``, or without them when it takes none."""
        if _takes_second(self.train):
            return self.train(frame, dict(settings))
        return self.train(frame)

    def metrics(self, model: Any, frame: Any) -> dict[str, float]:
        """``score`` on ``frame``, kept to its numbers. Raises `TrainError` for anything else."""
        given = self.score(model, frame)
        if not isinstance(given, Mapping):
            raise TrainError.bad_metrics(given)
        kept: dict[str, float] = {}
        for key, value in given.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TrainError.bad_metrics(given)
            kept[str(key)] = float(value)
        return kept

    def write(self, model: Any, directory: Path) -> Path:
        """The file ``save`` wrote for ``model`` in ``directory``, or the default's. Raises `TrainError`."""
        if self.save is None:
            return default_save(model, directory)
        written = self.save(model, directory)
        path = Path(written) if isinstance(written, (str, Path)) else None
        if path is None or not path.exists():
            raise TrainError.saved_nothing(written)
        return path


def _takes_second(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` accepts a second positional argument."""
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    positional = [
        p
        for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
    ]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in positional)


def default_save(model: Any, directory: Path) -> Path:
    """Write a mapping of numpy arrays as ``model.npz`` in ``directory``; refuse anything else."""
    try:
        import numpy as np  # optional dependency: numpy comes with the `ml` extra
    except ImportError as exc:
        raise TrainError.cannot_save(model) from exc
    if not isinstance(model, Mapping) or not model:
        raise TrainError.cannot_save(model)
    arrays: dict[str, Any] = {}
    for key, value in model.items():
        if isinstance(value, bool) or not isinstance(value, (np.ndarray, int, float)):
            raise TrainError.cannot_save(model)
        arrays[str(key)] = np.asarray(value)
    path = directory / DEFAULT_MODEL_FILE
    np.savez(path, **arrays)
    return path


def load_recipe(target: str) -> Recipe:
    """The recipe ``target`` names: a module's name, or a ``.py`` file. Raises `TrainError`."""
    name = target.strip()
    if not name:
        raise TrainError.no_recipe()
    try:
        module = _module(name)
    except Exception as exc:
        raise TrainError.recipe_not_found(name, exc) from exc
    train = getattr(module, "train", None)
    score = getattr(module, "score", None)
    save = getattr(module, "save", None)
    if not callable(train):
        raise TrainError.no_train(name)
    if not callable(score):
        raise TrainError.no_score(name)
    path = _source_of(module)
    return Recipe(
        name=name,
        path=path,
        code_sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path else "",
        train=train,
        score=score,
        save=save if callable(save) else None,
    )


def _module(name: str) -> Any:
    """The module ``name`` names; a file's directory joins the path, so it may import what is beside it."""
    path = Path(name)
    if name.endswith(".py") or path.is_file():
        resolved = path.resolve()
        beside = str(resolved.parent)
        if beside not in sys.path:
            sys.path.insert(0, beside)
        spec = importlib.util.spec_from_file_location(
            f"wactorz_recipe_{hashlib.sha256(str(resolved).encode()).hexdigest()[:12]}", resolved
        )
        if spec is None or spec.loader is None:
            raise NoLoaderError(name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(name)


def _source_of(module: Any) -> Path | None:
    file = getattr(module, "__file__", None)
    if not file:
        return None
    path = Path(file)
    return path if path.is_file() else None
