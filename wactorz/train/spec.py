# SPDX-License-Identifier: Apache-2.0
"""What one training run is asked to do, and why it may be refused.

A `TrainSpec` is the whole of a run as the asker gives it: the recipe, the
model's name, the source and window to read, the split, the seed and settings,
the metric and margin a move depends on, the pointer the run is for, and the
limits the child runs under. It is JSON both ways, so an agent's task, a
terminal command and the file the child reads are one shape. ``docs/design/
train-step.md`` is the decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.artifacts import name_error

#: How a run cuts its window: the last ``fraction`` of the rows held out, a cut
#: at one moment, ``k`` forward folds, or ``k`` segments held out at a time.
HOLDOUT = "holdout"
TIME_SPLIT = "time_split"
ROLLING_ORIGIN = "rolling_origin"
LEAVE_K_OUT = "leave_k_out"
SPLIT_KINDS = (HOLDOUT, TIME_SPLIT, ROLLING_ORIGIN, LEAVE_K_OUT)

#: The pointer a run is for: one of the store's two, or none to store only.
NONE = "none"
POINTERS = ("shadow", "active", NONE)

#: The files a run's directory holds: what the child is told, what it answered, what it printed.
SPEC_FILE = "spec.json"
RESULT_FILE = "result.json"
LOG_FILE = "log.txt"

#: What a run takes when not told.
DEFAULT_SEED = 7
DEFAULT_FRACTION = 0.2
DEFAULT_TIMEOUT_S = 3600.0
DEFAULT_WINDOW_S = 7 * 86400.0


class TrainError(ValueError):
    """A run that cannot be made from what it was given, and why."""

    @classmethod
    def bad_name(cls, error: str) -> TrainError:
        """The error for a model name refused, with ``error`` saying why."""
        return cls(error)

    @classmethod
    def no_recipe(cls) -> TrainError:
        """The error for a run with no recipe."""
        return cls("a run needs a recipe: a module name or a .py file")

    @classmethod
    def no_source(cls) -> TrainError:
        """The error for a run with no source to read."""
        return cls("a run needs a source to read")

    @classmethod
    def bad_window(cls, since: Any, until: Any) -> TrainError:
        """The error for a window that is not ``[since, until)`` in Unix seconds with ``since``
        before ``until``.
        """
        return cls(
            f"the window must be [since, until) in Unix seconds, since < until: {since!r}, {until!r}"
        )

    @classmethod
    def unknown_split(cls, kind: Any) -> TrainError:
        """The error for a split ``kind`` that is none of the known kinds."""
        return cls(f"a split is one of {', '.join(SPLIT_KINDS)}, not {kind!r}")

    @classmethod
    def split_needs(cls, kind: str, what: str) -> TrainError:
        """The error for a split of ``kind`` that was not given ``what`` it needs."""
        return cls(f"a {kind} split needs {what}")

    @classmethod
    def unknown_pointer(cls, pointer: Any) -> TrainError:
        """The error for a run for a ``pointer`` that is none of the pointers a run can move."""
        return cls(f"a run is for one of {', '.join(POINTERS)}, not {pointer!r}")

    @classmethod
    def needs_metric(cls, pointer: str) -> TrainError:
        """The error for a run for ``pointer`` with no metric to compare versions on."""
        return cls(f"a run for {pointer} needs a metric to compare on; say which, or set none")

    @classmethod
    def not_a_number(cls, what: str, value: Any) -> TrainError:
        """The error for a setting ``what`` given a ``value`` that is not a number."""
        return cls(f"{what} must be a number, not {value!r}")

    @classmethod
    def split_not_a_mapping(cls, value: Any) -> TrainError:
        """The error for a split given as something other than a mapping."""
        return cls(f"the split must be a mapping, not {value!r}")

    @classmethod
    def settings_not_a_mapping(cls, value: Any) -> TrainError:
        """The error for settings given as something other than a mapping."""
        return cls(f"the settings must be a mapping, not {value!r}")

    @classmethod
    def settings_not_json(cls, exc: Exception) -> TrainError:
        """The error for settings text that does not parse as JSON."""
        return cls(f"the settings are not JSON: {exc}")

    @classmethod
    def no_train(cls, recipe: str) -> TrainError:
        """The error for a ``recipe`` with no ``train(frame, settings)``."""
        return cls(f"{recipe} defines no train(frame, settings)")

    @classmethod
    def no_score(cls, recipe: str) -> TrainError:
        """The error for a ``recipe`` with no ``score(model, frame)``."""
        return cls(f"{recipe} defines no score(model, frame)")

    @classmethod
    def recipe_not_found(cls, recipe: str, exc: Exception) -> TrainError:
        """The error for a ``recipe`` that cannot be loaded, with the reason ``exc``."""
        return cls(f"cannot load the recipe {recipe}: {exc}")

    @classmethod
    def cannot_save(cls, model: Any) -> TrainError:
        """The error for a ``model`` that ``train()`` returned and the step cannot save without
        the recipe's ``save``.
        """
        return cls(
            f"train() returned a {type(model).__name__}, which the step cannot save on its own: "
            "return a mapping of numpy arrays, or define save(model, directory) -> path"
        )

    @classmethod
    def saved_nothing(cls, path: Any) -> TrainError:
        """The error for a ``save()`` that returned ``path``, which is not a file it wrote."""
        return cls(f"save() returned {path!r}, which is not a file it wrote")

    @classmethod
    def bad_metrics(cls, value: Any) -> TrainError:
        """The error for a ``score()`` that returned ``value``, which is not a mapping of
        numbers.
        """
        return cls(f"score() must return a mapping of numbers, not {value!r}")

    @classmethod
    def no_such_metric(cls, metric: str, had: list[str]) -> TrainError:
        """The error for a ``score()`` without the ``metric`` the run compares on; ``had`` is
        what it gave.
        """
        return cls(f"score() gave no {metric!r}; it gave {', '.join(had) or 'nothing'}")

    @classmethod
    def nothing_to_train_on(cls, source: str, since: float, until: float) -> TrainError:
        """The error for a ``source`` with no rows in the window ``[since, until)``."""
        return cls(f"{source} has no rows in [{since:.0f}, {until:.0f})")

    @classmethod
    def nothing_to_fit(cls) -> TrainError:
        """The error for a split that leaves no rows to train on."""
        return cls("the split leaves nothing to train on")

    @classmethod
    def nothing_to_score(cls) -> TrainError:
        """The error for a split that leaves no rows to score on."""
        return cls("the split leaves nothing to score on")

    @classmethod
    def timed_out(cls, seconds: float) -> TrainError:
        """The error for a run stopped after its timeout of ``seconds``."""
        return cls(f"the run was stopped after {seconds:g} s (timeout)")

    @classmethod
    def child_failed(cls, code: int | None, log: Any) -> TrainError:
        """The error for a training process that ended with ``code`` and wrote no result; ``log``
        is where it wrote.
        """
        return cls(f"the training process ended with code {code} and no result; see {log}")

    @classmethod
    def no_result(cls, error: str) -> TrainError:
        """The error the training process reported, ``error``, in its result."""
        return cls(error)

    @classmethod
    def in_progress(cls, run_id: str) -> TrainError:
        """The error for a run asked for while the run ``run_id`` is still going."""
        return cls(f"a run is in progress ({run_id}); ask again when it has ended")

    @classmethod
    def not_on_main(cls) -> TrainError:
        """The error for a run asked of anything but main."""
        return cls("the trainer runs on main, beside the store and the data directory")


def _number(what: str, value: Any, default: float | None = None) -> float:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrainError.not_a_number(what, value)
    return float(value)


@dataclass(frozen=True)
class SplitSpec:
    """How a run cuts its window into what is trained on and what is scored on."""

    kind: str = HOLDOUT
    #: `HOLDOUT`: the share of rows, at the end, that are scored on.
    fraction: float = DEFAULT_FRACTION
    #: `TIME_SPLIT`: the moment the cut is made at, in event time.
    at: float | None = None
    #: `ROLLING_ORIGIN` and `LEAVE_K_OUT`: how many folds, or segments held out.
    k: int = 1
    #: `LEAVE_K_OUT`: how segments are cut (`wactorz.data.frames.segments`), and its parameter.
    by: str = "gap"
    gap_s: float | None = None
    length_s: float | None = None
    column: str | None = None
    #: `LEAVE_K_OUT`: the seed that samples the folds when there are too many.
    seed: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The split as JSON, with only what its kind reads."""
        kept: dict[str, Any] = {"kind": self.kind}
        if self.kind == HOLDOUT:
            kept["fraction"] = self.fraction
        elif self.kind == TIME_SPLIT:
            kept["at"] = self.at
        elif self.kind == ROLLING_ORIGIN:
            kept["k"] = self.k
        else:
            kept.update({"k": self.k, "by": self.by, "seed": self.seed})
            for name in ("gap_s", "length_s", "column"):
                value = getattr(self, name)
                if value is not None:
                    kept[name] = value
        return kept

    @classmethod
    def from_dict(cls, given: Any) -> SplitSpec:
        """A split as a task or a file gives it; ``None`` is the default. Raises `TrainError`."""
        if given is None:
            return cls()
        if isinstance(given, str):
            given = {"kind": given}
        if not isinstance(given, Mapping):
            raise TrainError.split_not_a_mapping(given)
        kind = str(given.get("kind") or HOLDOUT)
        if kind not in SPLIT_KINDS:
            raise TrainError.unknown_split(given.get("kind"))
        at = given.get("at")
        if kind == TIME_SPLIT and at is None:
            raise TrainError.split_needs(kind, "at, the moment to cut at")
        k = given.get("k", 1)
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise TrainError.split_needs(kind, "k, a whole number of folds")
        seed = given.get("seed")
        return cls(
            kind=kind,
            fraction=_number("fraction", given.get("fraction"), DEFAULT_FRACTION),
            at=None if at is None else _number("at", at),
            k=k,
            by=str(given.get("by") or "gap"),
            gap_s=None if given.get("gap_s") is None else _number("gap_s", given.get("gap_s")),
            length_s=None
            if given.get("length_s") is None
            else _number("length_s", given.get("length_s")),
            column=str(given["column"]) if given.get("column") else None,
            seed=int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else None,
        )


@dataclass(frozen=True)
class TrainSpec:
    """One run, as the asker gives it."""

    recipe: str
    model: str
    source: str
    since: float
    until: float
    split: SplitSpec = field(default_factory=SplitSpec)
    seed: int = DEFAULT_SEED
    settings: dict[str, Any] = field(default_factory=dict)
    #: The figure of ``score()`` the move depends on; "" compares nothing.
    metric: str = ""
    margin: float = 0.0
    higher_is_better: bool = True
    #: The pointer the run is for: moved when the margin is met.
    pointer: str = "shadow"
    #: Whether the frame carries the labels; None joins them when the version
    #: compared against says what it is judged on.
    labels: bool | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: An address-space limit for the child, in MiB; 0 for none.
    memory_mb: int = 0
    #: Why the run was asked for, in the asker's words.
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.recipe.strip():
            raise TrainError.no_recipe()
        error = name_error(self.model)
        if error:
            raise TrainError.bad_name(error)
        if not self.source.strip():
            raise TrainError.no_source()
        if not (0 <= self.since < self.until):
            raise TrainError.bad_window(self.since, self.until)
        if self.pointer not in POINTERS:
            raise TrainError.unknown_pointer(self.pointer)
        if self.pointer != NONE and not self.metric:
            raise TrainError.needs_metric(self.pointer)
        try:
            json.dumps(self.settings)
        except (TypeError, ValueError) as exc:
            raise TrainError.settings_not_json(exc) from exc

    @property
    def moves(self) -> bool:
        """Whether the run moves a pointer when the margin is met."""
        return self.pointer != NONE

    def to_dict(self) -> dict[str, Any]:
        """The spec as JSON, as :meth:`from_dict` reads it back."""
        return {
            "recipe": self.recipe,
            "model": self.model,
            "source": self.source,
            "since": self.since,
            "until": self.until,
            "split": self.split.to_dict(),
            "seed": self.seed,
            "settings": dict(self.settings),
            "metric": self.metric,
            "margin": self.margin,
            "higher_is_better": self.higher_is_better,
            "pointer": self.pointer,
            "labels": self.labels,
            "timeout_s": self.timeout_s,
            "memory_mb": self.memory_mb,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, given: Mapping[str, Any]) -> TrainSpec:
        """A run as a task or a file gives it, checked. Raises `TrainError` saying what is wrong."""
        settings = given.get("settings") or {}
        if not isinstance(settings, Mapping):
            raise TrainError.settings_not_a_mapping(settings)
        seed = given.get("seed", DEFAULT_SEED)
        memory = given.get("memory_mb", 0)
        labels = given.get("labels")
        return cls(
            recipe=str(given.get("recipe") or ""),
            model=str(given.get("model") or ""),
            source=str(given.get("source") or ""),
            since=_number("since", given.get("since")),
            until=_number("until", given.get("until")),
            split=SplitSpec.from_dict(given.get("split")),
            seed=int(seed)
            if isinstance(seed, int) and not isinstance(seed, bool)
            else DEFAULT_SEED,
            settings=dict(settings),
            metric=str(given.get("metric") or ""),
            margin=_number("margin", given.get("margin"), 0.0),
            higher_is_better=bool(given.get("higher_is_better", True)),
            pointer=str(given.get("pointer") or "shadow"),
            labels=None if labels is None else bool(labels),
            timeout_s=_number("timeout_s", given.get("timeout_s"), DEFAULT_TIMEOUT_S),
            memory_mb=int(memory)
            if isinstance(memory, int) and not isinstance(memory, bool)
            else 0,
            reason=str(given.get("reason") or ""),
        )


def verdict(
    new: Mapping[str, Any],
    parent: Mapping[str, Any] | None,
    *,
    metric: str,
    margin: float,
    higher_is_better: bool = True,
) -> tuple[bool, str]:
    """Whether the new version beats the parent by the margin on ``metric``, and the words for it.

    With no parent there is nothing to beat, and the move is made. Raises
    `TrainError` when either side's figures lack the metric.
    """
    if metric not in new:
        raise TrainError.no_such_metric(metric, sorted(new))
    mine = float(new[metric])
    if parent is None:
        return True, f"{metric} {mine:.4g}, no version to compare against"
    if metric not in parent:
        raise TrainError.no_such_metric(metric, sorted(parent))
    theirs = float(parent[metric])
    gain = (mine - theirs) if higher_is_better else (theirs - mine)
    met = gain >= margin
    words = f"{metric} {mine:.4g} vs {theirs:.4g} (margin {margin:g} "
    words += "met)" if met else f"short by {margin - gain:.4g})"
    return met, words
