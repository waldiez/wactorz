# SPDX-License-Identifier: Apache-2.0
"""One training run, from the parent's side: start the child, store what it made, move or not.

`run` is what the trainer agent and ``wactorz-train`` both call. It makes the
run's directory under the state directory, writes the child's specification,
starts ``python -m wactorz.train.worker`` with the run's timeout, reads the
result back, stores the model with the metadata a version carries
(``docs/design/train-step.md``), moves the pointer the run is for when the
margin is met, and writes the run and the version into the graph when it is
given one. It blocks on nothing: the store, the graph and the wait on the
child are all off the event loop, through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..core.artifacts import Artifact, ArtifactError, ArtifactStore, Pointer
from ..core.judgement import Judgement
from ..core.labels import Interval
from ..core.provenance import Provenance
from ..data.files import data_root
from ..data.retention import DATA_KEY
from .record import record_failure, record_model, record_start
from .spec import LOG_FILE, RESULT_FILE, SPEC_FILE, TrainError, TrainSpec, verdict

logger: logging.Logger = logging.getLogger(__name__)

#: Where runs are kept under the state directory: the specification, the
#: result and the log of each, for a person to read after.
RUNS_DIRNAME = "train-runs"

#: How many run directories are kept; the oldest go as new ones are made.
RUNS_KEPT = 50

#: The metadata key saying the step stored the version.
TRAINED_BY = "wactorz-train"

#: How long the child is given to end after being told to, before it is killed.
GRACE_S = 5.0

#: What reads a source's labels over a window: `Labels.effective`, or a stand-in.
LabelsOf = Callable[[str, float, float], Sequence[Interval]]


@dataclass
class Outcome:
    """What a run came to: the version stored, the move made or refused, or the failure."""

    run_id: str
    run_dir: Path
    spec: TrainSpec
    ok: bool = False
    error: str | None = None
    artifact: Artifact | None = None
    parent: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    moved: Pointer | None = None
    #: Why the pointer was not moved, when it was not: the margin, or the store's refusal.
    not_moved: str | None = None
    seconds: float = 0.0

    @property
    def reference(self) -> str | None:
        """The stored version's reference, or None when the run stored nothing."""
        return self.artifact.reference if self.artifact else None

    def summary(self) -> str:
        """One line for the feed."""
        if not self.ok:
            return f"training {self.spec.model} failed: {self.error}"
        line = f"stored {self.reference}"
        if self.moved is not None:
            line += f", now {self.moved.pointer}"
            if self.moved.reason:
                line += f": {self.moved.reason}"
        elif self.not_moved:
            line += f", not moved: {self.not_moved}"
        return line

    def to_dict(self) -> dict[str, Any]:
        """The outcome as JSON: the train agent's reply, and what ``wactorz-train`` prints."""
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "error": self.error,
            "model": self.spec.model,
            "version": self.artifact.version if self.artifact else None,
            "reference": self.reference,
            "parent": self.parent,
            "metrics": dict(self.metrics),
            "moved": self.moved.payload() if self.moved else None,
            "not_moved": self.not_moved,
            "seconds": self.seconds,
            "run_dir": str(self.run_dir),
        }


def _with_absolute_recipe(spec: TrainSpec) -> TrainSpec:
    """``spec`` with a recipe file named by its absolute path.

    A relative path is read where the run was asked for, not where the child
    runs, which is the state directory; a module name is left as it is.
    """
    recipe = spec.recipe.strip()
    path = Path(recipe)
    if (recipe.endswith(".py") or path.is_file()) and not path.is_absolute():
        return replace(spec, recipe=str(path.resolve()))
    return spec


def new_run_id(now: float) -> str:
    """A run's id: when it started, in microseconds, and a few random characters to tell two apart.

    The time is zero-padded so the ids sort as the runs started, which is how
    `_prune_runs` finds the oldest; runs started in the same second still sort
    in order. An id of whole seconds sorts before these.
    """
    return f"{round(now * 1_000_000):016d}-{uuid.uuid4().hex[:8]}"


def runs_root(state_dir: str | Path) -> Path:
    """The directory under ``state_dir`` that every run's directory is made in."""
    return Path(state_dir) / RUNS_DIRNAME


def _parent_of(store: ArtifactStore, name: str) -> dict[str, Any] | None:
    """The version compared against, pinned with its path: ``active``, else the newest; None for none."""
    versions = store.versions(name)
    if not versions:
        return None
    pointer = store.pointer(name, "active")
    reference = pointer.reference if pointer is not None else versions[-1].reference
    pinned = store.pin(reference)
    return {**pinned, "reference": reference, "path": str(store.readable_path(pinned))}


def _judgement_of(store: ArtifactStore, parent: dict[str, Any] | None) -> Judgement | None:
    """What the version compared against says it is judged on; None when nothing says."""
    if parent is None:
        return None
    try:
        return store.resolve(parent["reference"]).judgement
    except ArtifactError:
        return None


def _prune_runs(root: Path, keep: int) -> None:
    """Let the oldest run directories go, keeping ``keep``; their names sort as they started (`new_run_id`)."""
    if not root.is_dir():
        return
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in runs[: max(0, len(runs) - keep)]:
        for path in sorted(old.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
        with contextlib.suppress(OSError):
            old.rmdir()


def metadata_for(spec: TrainSpec, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """What the stored version carries, as ``docs/design/train-step.md`` lists it."""
    metrics = result.get("metrics", {})
    return {
        "trained_by": TRAINED_BY,
        "recipe": spec.recipe,
        "code_sha256": result.get("code_sha256", ""),
        "seed": spec.seed,
        "settings": dict(spec.settings),
        "window": {"source": spec.source, "since": spec.since, "until": spec.until},
        DATA_KEY: [{"source": spec.source, "since": spec.since, "until": spec.until}],
        "split": result.get("split", {}),
        "rows": result.get("rows", {}),
        "metrics": {
            "new": metrics.get("new", {}),
            "parent": metrics.get("parent"),
            "metric": spec.metric,
            "margin": spec.margin,
            "higher_is_better": spec.higher_is_better,
        },
        "labelled": bool(result.get("labelled")),
        "run_id": run_id,
        "train_seconds": metrics.get("train_seconds"),
    }


def _wait_for_child(command: list[str], *, cwd: Path, log: Path, timeout_s: float) -> int | None:
    """Run ``command`` to its end, or kill it at ``timeout_s``; its exit code, None when killed.

    Through ``subprocess`` on a thread, not asyncio's subprocess support: the
    selector event loop Wactorz runs on Windows has none, and a plain process
    waited on off the loop works the same everywhere.
    """
    with log.open("ab") as out:
        process = subprocess.Popen(  # noqa: S603  # a literal argv naming this interpreter
            command, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT
        )
        try:
            return process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=GRACE_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return None


async def _await_child(command: list[str], *, cwd: Path, log: Path, timeout_s: float) -> int | None:
    """`_wait_for_child`, off the event loop."""
    return await asyncio.to_thread(_wait_for_child, command, cwd=cwd, log=log, timeout_s=timeout_s)


async def run(
    spec: TrainSpec,
    *,
    state_dir: str | Path,
    by: str,
    prov: Provenance | None = None,
    labels_of: LabelsOf | None = None,
    python: str | None = None,
    now: float | None = None,
) -> Outcome:
    """Carry out ``spec``: fit in a child, store, move when the margin is met, record.

    ``by`` is who asked, as the move and the graph record it. ``prov`` is the
    graph to write, when there is one; ``labels_of`` reads a source's labels
    over a window, for the frame's label column and the ``trained`` edges.
    Never raises for a run that failed: the outcome says why.
    """
    started = time.time() if now is None else now
    run_id = new_run_id(started)
    state = Path(state_dir)
    run_dir = runs_root(state) / run_id
    spec = _with_absolute_recipe(spec)
    outcome = Outcome(run_id=run_id, run_dir=run_dir, spec=spec)
    store = ArtifactStore.in_state_dir(state)
    try:
        parent = await asyncio.to_thread(_parent_of, store, spec.model)
    except ArtifactError as exc:
        outcome.error = f"the version to compare against cannot be read: {exc}"
        return outcome
    outcome.parent = parent["reference"] if parent else None
    judgement = _judgement_of(store, parent)
    wants_labels = spec.labels if spec.labels is not None else judgement is not None
    intervals: Sequence[Interval] = ()
    if wants_labels and labels_of is not None:
        # The labels of the source the model is judged against when it names
        # one, else of the recorded source itself.
        label_source = judgement.source if judgement and judgement.source else spec.source
        intervals = await asyncio.to_thread(labels_of, label_source, spec.since, spec.until)
    if prov is not None:
        await asyncio.to_thread(record_start, prov, run_id=run_id, spec=spec, by=by, at=started)
    await asyncio.to_thread(_write_spec, run_dir, spec, run_id, state, parent, intervals)
    command = [python or sys.executable, "-m", "wactorz.train.worker", str(run_dir)]
    try:
        code = await _await_child(
            command, cwd=state, log=run_dir / LOG_FILE, timeout_s=spec.timeout_s
        )
        outcome.seconds = time.time() - started
        result = await asyncio.to_thread(_read_result, run_dir, code, spec.timeout_s)
        artifact = await asyncio.to_thread(_store, store, spec, run_id, result)
    except (TrainError, ArtifactError, OSError) as exc:
        outcome.error = str(exc)
        if prov is not None:
            await asyncio.to_thread(
                record_failure, prov, run_id=run_id, error=outcome.error, at=time.time()
            )
        return outcome
    outcome.ok = True
    outcome.artifact = artifact
    outcome.metrics = dict(artifact.metadata.get("metrics", {}))
    metrics = result.get("metrics", {})
    if spec.moves:
        try:
            met, words = verdict(
                metrics.get("new", {}),
                metrics.get("parent"),
                metric=spec.metric,
                margin=spec.margin,
                higher_is_better=spec.higher_is_better,
            )
        except TrainError as exc:
            met, words = False, str(exc)
        if met:
            reason = f"{words}; {spec.reason}" if spec.reason else words
            try:
                outcome.moved = await asyncio.to_thread(
                    store.move, spec.pointer, artifact.reference, by=by, reason=reason
                )
            except ArtifactError as exc:
                outcome.not_moved = str(exc)
        else:
            outcome.not_moved = words
    if prov is not None:
        await asyncio.to_thread(
            record_model,
            prov,
            run_id=run_id,
            artifact=artifact,
            metrics=outcome.metrics,
            label_ids=[i.label.id for i in intervals],
            moved=outcome.moved,
            at=time.time(),
        )
    await asyncio.to_thread(_prune_runs, runs_root(state), RUNS_KEPT)
    return outcome


def _write_spec(
    run_dir: Path,
    spec: TrainSpec,
    run_id: str,
    state: Path,
    parent: dict[str, Any] | None,
    intervals: Sequence[Interval],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "run": spec.to_dict(),
        "run_id": run_id,
        "data_root": str(data_root(state)),
        "parent": parent,
        "labels": [
            {
                "start": i.start,
                "end": i.end,
                "value": i.label.value,
                "label_id": i.label.id,
                "actor": i.label.actor,
            }
            for i in intervals
        ],
    }
    (run_dir / SPEC_FILE).write_text(json.dumps(body, indent=2), encoding="utf-8")


def _read_result(run_dir: Path, code: int | None, timeout_s: float) -> dict[str, Any]:
    """What the child wrote. Raises `TrainError` for a run that made nothing, saying why."""
    if code is None:
        raise TrainError.timed_out(timeout_s)
    path = run_dir / RESULT_FILE
    if not path.is_file():
        raise TrainError.child_failed(code, run_dir / LOG_FILE)
    result = json.loads(path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise TrainError.no_result(str(result.get("error") or "the run made nothing"))
    return result


def _store(store: ArtifactStore, spec: TrainSpec, run_id: str, result: dict[str, Any]) -> Artifact:
    """Store the model the child saved as the next version, and let the child's copy go."""
    path = Path(result["model_path"])
    artifact = store.add(path, spec.model, metadata=metadata_for(spec, run_id, result))
    with contextlib.suppress(OSError):
        path.unlink()
    return artifact
