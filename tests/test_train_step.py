"""The train step makes a version inside the loop: a recipe fitted in a child, stored, moved by a margin.

A recipe is a module with ``train`` and ``score``. A run reads a window
through the loader, cuts it with one of the loader's splits, fits and scores
in a child process with a timeout, stores the version with what it was
trained on, moves the pointer it is for only when the margin is met, and
writes the run and the version into the graph. ``docs/design/train-step.md``
is the decision these pin.
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from wactorz.core.artifacts import ArtifactStore
from wactorz.core.judgement import declared
from wactorz.core.labels import Labels
from wactorz.core.model_swaps import move_id
from wactorz.core.paths import resolve_state_dir
from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import (
    ACTION,
    DONE,
    EVALUATED_BY,
    FAILED,
    LED_TO,
    MODEL,
    OUTCOME,
    PRODUCED,
    RUNNING,
    TRAINED,
    Provenance,
)
from wactorz.core.shadow_trial import CLEARED, record_outcome
from wactorz.data import data_root
from wactorz.data.parquet import Writer
from wactorz.data.retention import protected_windows
from wactorz.train import Outcome, TrainError, TrainSpec, model_node_id
from wactorz.train import run as run_module
from wactorz.train.recipe import default_save, load_recipe
from wactorz.train.record import TRAIN, TRAIN_FAILED, record_start, run_node_id, runs_of
from wactorz.train.run import run
from wactorz.train.spec import (
    HOLDOUT,
    LEAVE_K_OUT,
    RESULT_FILE,
    ROLLING_ORIGIN,
    SPEC_FILE,
    TIME_SPLIT,
    SplitSpec,
    verdict,
)
from wactorz.train.splits import folds_for
from wactorz.train.worker import limit_memory, mean_of, work
from wactorz.train.worker import main as worker_main

T0 = 1_760_000_000.0
#: Readings a minute apart, the window they fill, and a label over its second half.
ROWS = 120
UNTIL = T0 + ROWS * 60
SOURCE = "sensors/x"

RECIPE = '''
"""The mean of x; scored by how far a window's mean sits from it, lower being better."""
import time

import numpy as np


def train(frame, settings):
    x = np.asarray(frame.column("x").to_pylist(), dtype=float)
    if settings.get("sleep"):
        time.sleep(float(settings["sleep"]))
    if settings.get("fail"):
        raise RuntimeError("the recipe refused")
    offset = float(settings.get("offset", 0.0))
    return {"mean": np.array([x.mean() + offset]), "labelled": np.array([int("label" in frame.column_names)])}


def score(model, frame):
    x = np.asarray(frame.column("x").to_pylist(), dtype=float)
    return {"error": float(abs(x.mean() - model["mean"][0])), "rows": frame.num_rows}
'''


@pytest.fixture(name="state")
def state_fixture() -> Path:
    return Path(resolve_state_dir())


@pytest.fixture(name="recorded")
def recorded_fixture(state: Path) -> Path:
    """``ROWS`` readings of ``SOURCE``, a minute apart from ``T0``, ``x`` wobbling around one."""
    writer = Writer(data_root(state), SOURCE)
    for i in range(ROWS):
        writer.append(
            {
                "ts": T0 + i * 60,
                "arrived": T0 + i * 60,
                "time_source": "payload",
                "x": 1.0 + (i % 7) / 10,
            }
        )
    writer.close()
    return data_root(state)


@pytest.fixture(name="recipe")
def recipe_fixture(tmp_path: Path) -> str:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE)
    return str(path)


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


def _spec(recipe: str, **over: Any) -> TrainSpec:
    given: dict[str, Any] = {
        "recipe": recipe,
        "model": "demo",
        "source": SOURCE,
        "since": T0,
        "until": UNTIL,
        "metric": "error",
        "higher_is_better": False,
        "timeout_s": 60.0,
    }
    given.update(over)
    return TrainSpec.from_dict(given)


def _frame(n: int, step: float = 60.0) -> pa.Table:
    return pa.table({"ts": [T0 + i * step for i in range(n)], "x": [float(i) for i in range(n)]})


# ── The specification ─────────────────────────────────────────────────────────


class TestTheSpec:
    def test_a_task_fills_in_what_it_leaves_out(self, recipe: str) -> None:
        spec = _spec(recipe)

        assert (spec.split.kind, spec.seed, spec.pointer, spec.margin, spec.labels) == (
            HOLDOUT,
            7,
            "shadow",
            0.0,
            None,
        )
        assert TrainSpec.from_dict(spec.to_dict()) == spec

    @pytest.mark.parametrize(
        ("over", "words"),
        [
            ({"recipe": ""}, "needs a recipe"),
            ({"model": "no:colon"}, "no:colon"),
            ({"source": ""}, "needs a source"),
            ({"since": UNTIL}, "since < until"),
            ({"pointer": "latest"}, "shadow, active, none"),
            ({"metric": ""}, "needs a metric"),
            ({"split": "shuffle"}, "not 'shuffle'"),
            ({"split": {"kind": TIME_SPLIT}}, "needs at"),
            ({"split": {"kind": ROLLING_ORIGIN, "k": 0}}, "whole number of folds"),
            ({"settings": [1]}, "settings must be a mapping"),
            ({"margin": "lots"}, "must be a number"),
        ],
    )
    def test_what_cannot_be_a_run_is_refused_saying_why(
        self, recipe: str, over: dict[str, Any], words: str
    ) -> None:
        with pytest.raises(TrainError, match=words):
            TrainSpec.from_dict({**_spec(recipe).to_dict(), **over})

    def test_a_run_for_no_pointer_needs_no_metric(self, recipe: str) -> None:
        spec = _spec(recipe, pointer="none", metric="")

        assert spec.moves is False

    def test_a_split_keeps_only_what_its_kind_reads(self) -> None:
        assert SplitSpec.from_dict(
            {"kind": LEAVE_K_OUT, "k": 2, "by": "length", "length_s": 600}
        ).to_dict() == {
            "kind": LEAVE_K_OUT,
            "k": 2,
            "by": "length",
            "seed": None,
            "length_s": 600.0,
        }
        assert SplitSpec.from_dict(None).to_dict() == {"kind": HOLDOUT, "fraction": 0.2}


class TestTheVerdict:
    def test_better_by_the_margin_is_met_and_says_the_figures(self) -> None:
        met, words = verdict({"f1": 0.91}, {"f1": 0.88}, metric="f1", margin=0.02)

        assert met is True
        assert words == "f1 0.91 vs 0.88 (margin 0.02 met)"

    def test_short_of_the_margin_says_by_how_much(self) -> None:
        met, words = verdict({"f1": 0.89}, {"f1": 0.88}, metric="f1", margin=0.02)

        assert met is False
        assert words == "f1 0.89 vs 0.88 (margin 0.02 short by 0.01)"

    def test_lower_may_be_better(self) -> None:
        met, _ = verdict(
            {"error": 0.1}, {"error": 0.3}, metric="error", margin=0.1, higher_is_better=False
        )

        assert met is True

    def test_nothing_to_compare_against_is_met(self) -> None:
        met, words = verdict({"f1": 0.5}, None, metric="f1", margin=0.5)

        assert (met, words) == (True, "f1 0.5, no version to compare against")

    def test_a_metric_score_did_not_give_is_refused(self) -> None:
        with pytest.raises(TrainError, match="gave no 'f1'; it gave error, rows"):
            verdict({"error": 0.1, "rows": 3}, None, metric="f1", margin=0.0)


# ── The recipe ────────────────────────────────────────────────────────────────


class TestTheRecipe:
    def test_a_file_is_loaded_with_its_hash(self, recipe: str) -> None:
        loaded = load_recipe(recipe)

        assert loaded.path == Path(recipe)
        assert len(loaded.code_sha256) == 64
        assert loaded.save is None
        assert callable(loaded.train) and callable(loaded.score)

    def test_a_module_is_loaded_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "my_recipe.py").write_text(RECIPE)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "my_recipe", raising=False)

        assert load_recipe("my_recipe").name == "my_recipe"

    @pytest.mark.parametrize(
        ("body", "words"),
        [
            ("def score(m, f): return {}", "defines no train"),
            ("def train(f, s): return {}", "defines no score"),
        ],
    )
    def test_a_module_missing_a_function_is_refused(
        self, tmp_path: Path, body: str, words: str
    ) -> None:
        path = tmp_path / "half.py"
        path.write_text(body)

        with pytest.raises(TrainError, match=words):
            load_recipe(str(path))

    def test_a_recipe_that_is_not_there_is_said(self, tmp_path: Path) -> None:
        with pytest.raises(TrainError, match="cannot load the recipe"):
            load_recipe(str(tmp_path / "missing.py"))

    def test_arrays_are_saved_as_npz_without_a_save(self, tmp_path: Path) -> None:
        import numpy as np

        written = default_save({"mean": np.array([1.0]), "k": 2}, tmp_path)

        assert written == tmp_path / "model.npz"
        with np.load(written) as arrays:
            assert sorted(arrays.files) == ["k", "mean"]

    def test_anything_else_needs_a_save(self, tmp_path: Path) -> None:
        with pytest.raises(TrainError, match="define save"):
            default_save(object(), tmp_path)

    def test_a_save_that_wrote_nothing_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "saving.py"
        path.write_text(
            "def train(f, s): return 1\ndef score(m, f): return {}\ndef save(m, d): return 'nowhere'\n"
        )

        with pytest.raises(TrainError, match="not a file it wrote"):
            load_recipe(str(path)).write(1, tmp_path)

    def test_train_may_take_the_frame_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "alone.py"
        path.write_text(
            "def train(f): return {'n': f.num_rows}\ndef score(m, f): return {'n': m['n']}\n"
        )
        loaded = load_recipe(str(path))

        assert loaded.fit(_frame(3), {"ignored": 1}) == {"n": 3}
        assert loaded.metrics({"n": 3}, _frame(1)) == {"n": 3.0}

    def test_score_must_give_numbers(self, tmp_path: Path) -> None:
        path = tmp_path / "words.py"
        path.write_text("def train(f, s): return 1\ndef score(m, f): return {'ok': 'yes'}\n")

        with pytest.raises(TrainError, match="mapping of numbers"):
            load_recipe(str(path)).metrics(1, _frame(1))


# ── The splits ────────────────────────────────────────────────────────────────


class TestTheSplits:
    def test_the_holdout_scores_on_the_last_fifth(self) -> None:
        (fold,) = folds_for(_frame(100), SplitSpec())

        assert (fold.train, fold.test, fold.kind) == (((0, 80),), ((80, 100),), HOLDOUT)
        assert fold.params["fraction"] == 0.2

    def test_a_time_split_cuts_at_the_moment(self) -> None:
        (fold,) = folds_for(_frame(10), SplitSpec(kind=TIME_SPLIT, at=T0 + 7 * 60))

        assert (fold.train, fold.test) == (((0, 7),), ((7, 10),))

    def test_forward_folds_and_segments_held_out(self) -> None:
        rolling = folds_for(_frame(10), SplitSpec(kind=ROLLING_ORIGIN, k=3))
        held = folds_for(_frame(10), SplitSpec(kind=LEAVE_K_OUT, k=1, by="length", length_s=300))

        assert [f.test for f in rolling] == [((1, 4),), ((4, 7),), ((7, 10),)]
        assert len(held) == 2 and all(f.kind == "leave_k_out" for f in held)

    def test_a_split_with_nothing_on_one_side_is_refused(self) -> None:
        with pytest.raises(TrainError, match="nothing to train on"):
            folds_for(_frame(0), SplitSpec())
        with pytest.raises(TrainError, match="nothing to score on"):
            folds_for(_frame(1), SplitSpec())
        with pytest.raises(TrainError, match="nothing to train on"):
            folds_for(_frame(5), SplitSpec(kind=TIME_SPLIT, at=T0))


# ── The worker, in this process ───────────────────────────────────────────────


def _write_spec(run_dir: Path, spec: TrainSpec, state: Path, **extra: Any) -> None:
    run_dir.mkdir(parents=True)
    body = {
        "run": spec.to_dict(),
        "run_id": run_dir.name,
        "data_root": str(data_root(state)),
        "parent": None,
        "labels": [],
        **extra,
    }
    (run_dir / SPEC_FILE).write_text(json.dumps(body))


class TestTheWorker:
    def test_it_fits_scores_saves_and_says_what_it_did(
        self, recipe: str, recorded: Path, state: Path, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        _write_spec(run_dir, _spec(recipe), state)

        result = work(run_dir)

        assert result["ok"] is True
        assert (run_dir / "model.npz").is_file()
        assert result["rows"] == {"window": ROWS, "train": 96, "test": 24}
        assert result["metrics"]["parent"] is None
        assert set(result["metrics"]["new"]) == {"error", "rows"}
        assert result["split"]["kind"] == HOLDOUT and len(result["split"]["folds"]) == 1
        assert result["seeded"][:2] == ["random", "numpy"]
        assert result["code_sha256"] == load_recipe(recipe).code_sha256

    def test_the_labels_the_parent_read_are_a_column(
        self, recipe: str, recorded: Path, state: Path, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        _write_spec(
            run_dir,
            _spec(recipe),
            state,
            labels=[{"start": T0 + 3600, "end": None, "value": "fault", "label_id": "l1"}],
        )

        result = work(run_dir)

        assert result["labelled"] is True
        import numpy as np

        with np.load(run_dir / "model.npz") as arrays:
            assert int(arrays["labelled"][0]) == 1

    def test_several_folds_store_a_model_of_every_row(
        self, recipe: str, recorded: Path, state: Path, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        _write_spec(run_dir, _spec(recipe, split={"kind": ROLLING_ORIGIN, "k": 3}), state)

        result = work(run_dir)

        assert len(result["metrics"]["folds"]) == 3
        assert result["metrics"]["new"]["rows"] == pytest.approx(ROWS / 3, abs=1)

    def test_a_window_with_no_rows_is_said(self, recipe: str, state: Path, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write_spec(run_dir, _spec(recipe), state)

        with pytest.raises(TrainError, match="has no rows"):
            work(run_dir)

    def test_main_writes_the_failure_as_the_result(
        self, recipe: str, state: Path, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        _write_spec(run_dir, _spec(recipe), state)

        assert worker_main([str(run_dir)]) == 1
        result = json.loads((run_dir / RESULT_FILE).read_text())
        assert result["ok"] is False and "has no rows" in result["error"]
        assert worker_main([]) == 2

    def test_the_mean_keeps_the_figures_every_fold_gave(self) -> None:
        assert mean_of([{"a": 1.0, "b": 2.0}, {"a": 3.0}]) == {"a": 2.0}
        assert mean_of([]) == {}

    def test_no_limit_applies_none(self) -> None:
        assert limit_memory(0) is False


# ── A run, child process and all ──────────────────────────────────────────────


async def _run(spec: TrainSpec, state: Path, **kwargs: Any) -> Outcome:
    return await run(spec, state_dir=state, by="test", **kwargs)


class TestARun:
    async def test_the_first_version_is_stored_with_its_record_and_moved(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        outcome = await _run(_spec(recipe, reason="a fresh start"), state)

        assert outcome.ok, outcome.error
        assert outcome.reference == "demo:1" and outcome.parent is None
        assert outcome.moved is not None
        assert (outcome.moved.pointer, outcome.moved.by) == ("shadow", "test")
        assert re.fullmatch(
            r"error 0\.0\d+, no version to compare against; a fresh start",
            outcome.moved.reason or "",
        )
        store = ArtifactStore.in_state_dir(state)
        meta = store.resolve("demo:1").metadata
        assert meta["trained_by"] == "wactorz-train"
        assert meta["data"] == [{"source": SOURCE, "since": T0, "until": UNTIL}]
        assert meta["window"] == {"source": SOURCE, "since": T0, "until": UNTIL}
        assert meta["split"]["kind"] == HOLDOUT and meta["split"]["folds"][0]["rows"] == {
            "train": 96,
            "test": 24,
        }
        assert meta["metrics"]["metric"] == "error" and meta["metrics"]["parent"] is None
        assert (meta["seed"], meta["run_id"], meta["recipe"]) == (7, outcome.run_id, recipe)
        assert meta["code_sha256"] == load_recipe(recipe).code_sha256
        assert protected_windows(store) == [(SOURCE, T0, UNTIL)]
        assert sorted(p.name for p in outcome.run_dir.iterdir()) == [
            "log.txt",
            "result.json",
            "spec.json",
        ]

    async def test_a_version_short_of_the_margin_is_stored_and_not_moved(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        await _run(_spec(recipe), state)
        ArtifactStore.in_state_dir(state).move("active", "demo:1", by="test")

        outcome = await _run(_spec(recipe, settings={"offset": 1.0}, margin=0.001), state)

        assert outcome.ok and outcome.reference == "demo:2" and outcome.parent == "demo:1"
        assert outcome.moved is None
        assert re.fullmatch(
            r"error 0\.9\d+ vs 0\.0\d+ \(margin 0\.001 short by 0\.9\d+\)", outcome.not_moved or ""
        )
        assert outcome.metrics["parent"]["error"] == pytest.approx(0.0, abs=0.05)
        assert ArtifactStore.in_state_dir(state).pointer("demo", "shadow") is None

    async def test_a_better_version_moves_the_pointer_with_the_figures(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        await _run(_spec(recipe, settings={"offset": 1.0}, pointer="active"), state)

        outcome = await _run(_spec(recipe, margin=0.5), state)

        assert outcome.moved is not None
        assert re.fullmatch(
            r"error 0\.0\d+ vs 0\.9\d+ \(margin 0\.5 met\)", outcome.moved.reason or ""
        )
        assert outcome.summary() == f"stored demo:2, now shadow: {outcome.moved.reason}"

    async def test_a_run_for_no_pointer_stores_only(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        outcome = await _run(_spec(recipe, pointer="none", metric=""), state)

        assert outcome.ok and outcome.moved is None and outcome.not_moved is None
        assert ArtifactStore.in_state_dir(state).pointers("demo") == {}

    async def test_the_run_and_the_version_are_in_the_graph(
        self, recipe: str, recorded: Path, state: Path, db: WactorzDB, graph: Provenance
    ) -> None:
        labels = Labels(db)
        inside = labels.write(
            source=SOURCE, start=T0 + 1800, end=T0 + 3600, value="fault", actor="operator"
        )
        outside = labels.write(source=SOURCE, start=UNTIL + 10, value="fault", actor="operator")
        spec = _spec(recipe, labels=True)

        outcome = await _run(
            spec,
            state,
            prov=graph,
            labels_of=lambda s, a, b: labels.effective(s, since=a, until=b),
        )

        assert outcome.ok and outcome.artifact is not None
        action = graph.get(run_node_id(outcome.run_id))
        assert action is not None
        assert (action.kind, action.status, action.actor, action.source) == (
            ACTION,
            DONE,
            "test",
            "models/demo",
        )
        assert action.payload["what"] == TRAIN and action.payload["window"]["since"] == T0
        model = graph.get(model_node_id("demo", 1, outcome.artifact.sha256))
        assert model is not None and model.kind == MODEL
        assert model.payload["metrics"]["new"]["error"] == pytest.approx(0.0, abs=0.05)
        relations = {(e.src, e.dst, e.relation) for e in graph.edges(model.id)}
        assert (action.id, model.id, PRODUCED) in relations
        assert (inside.id, model.id, TRAINED) in relations
        assert (outside.id, model.id, TRAINED) not in relations
        moved = outcome.moved
        assert moved is not None
        move = graph.get(move_id("demo", "shadow", 1, moved.moved_at))
        assert move is not None and move.payload["reason"] == moved.reason
        assert (action.id, move.id, LED_TO) in {
            (e.src, e.dst, e.relation) for e in graph.edges(move.id)
        }
        assert runs_of(graph, "demo")[0]["run_id"] == outcome.run_id
        assert ArtifactStore.in_state_dir(state).resolve("demo:1").metadata["labelled"] is True

    async def test_the_labels_are_joined_when_the_version_compared_against_is_judged(
        self, recipe: str, recorded: Path, state: Path, db: WactorzDB, graph: Provenance
    ) -> None:
        store = ArtifactStore.in_state_dir(state)
        first = await _run(_spec(recipe), state)
        assert first.ok
        store.judge("demo:1", declared(positive=["fault"], by="test"))
        reads: list[str] = []

        def effective(source: str, since: float, until: float) -> list[Any]:
            reads.append(source)
            return []

        second = await _run(_spec(recipe), state, labels_of=effective)

        assert second.ok and reads == [SOURCE]
        assert store.resolve("demo:2").judgement is not None

    async def test_the_labels_come_from_the_source_the_model_is_judged_against(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        store = ArtifactStore.in_state_dir(state)
        first = await _run(_spec(recipe), state)
        assert first.ok
        store.judge("demo:1", declared(positive=["fault"], source="sensors/imu/left", by="test"))
        reads: list[str] = []

        def effective(source: str, since: float, until: float) -> list[Any]:
            reads.append(source)
            return []

        second = await _run(_spec(recipe), state, labels_of=effective)

        assert second.ok and reads == ["sensors/imu/left"]

    async def test_a_relative_recipe_is_read_where_the_run_was_asked_for(
        self, recipe: str, recorded: Path, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(Path(recipe).parent)

        outcome = await _run(_spec("recipe.py"), state)

        assert outcome.ok, outcome.error
        assert outcome.spec.recipe == str(Path(recipe).resolve())
        meta = ArtifactStore.in_state_dir(state).resolve("demo:1").metadata
        assert meta["recipe"] == str(Path(recipe).resolve())

    async def test_a_recipe_that_fails_is_a_failed_action_with_its_reason(
        self, recipe: str, recorded: Path, state: Path, graph: Provenance
    ) -> None:
        outcome = await _run(_spec(recipe, settings={"fail": True}), state, prov=graph)

        assert outcome.ok is False and outcome.artifact is None
        assert outcome.error == "RuntimeError: the recipe refused"
        assert outcome.summary() == "training demo failed: RuntimeError: the recipe refused"
        action = graph.get(run_node_id(outcome.run_id))
        assert action is not None and action.status == FAILED
        (edge,) = graph.edges(action.id)
        assert edge.relation == EVALUATED_BY
        failure = graph.get(edge.dst)
        assert failure is not None and failure.kind == OUTCOME
        assert failure.payload == {
            "what": TRAIN_FAILED,
            "run_id": outcome.run_id,
            "error": outcome.error,
        }
        assert ArtifactStore.in_state_dir(state).names() == []
        assert "the recipe refused" in (outcome.run_dir / "log.txt").read_text()

    async def test_a_run_past_its_timeout_is_stopped_and_said(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        outcome = await _run(_spec(recipe, settings={"sleep": 30}, timeout_s=3.0), state)

        assert outcome.ok is False
        assert outcome.error == "the run was stopped after 3 s (timeout)"
        assert outcome.seconds < 20

    async def test_a_child_that_cannot_start_is_said(
        self, recipe: str, recorded: Path, state: Path
    ) -> None:
        outcome = await _run(_spec(recipe), state, python=sys.executable + "-missing")

        assert outcome.ok is False
        assert outcome.error is not None and "file" in outcome.error.lower()

    async def test_old_run_directories_go(
        self, recipe: str, recorded: Path, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(run_module, "RUNS_KEPT", 1)
        first = await _run(_spec(recipe), state)
        second = await _run(_spec(recipe), state)

        assert not first.run_dir.exists()
        assert second.run_dir.is_dir()


class TestTheGraphRecord:
    def test_a_running_action_is_about_the_window(self, graph: Provenance, recipe: str) -> None:
        node = record_start(graph, run_id="r1", spec=_spec(recipe), by="agent:trainer", at=T0 + 1)

        assert (node.status, node.ts, node.ts_end, node.recorded) == (RUNNING, T0, UNTIL, T0 + 1)
        assert node.id == run_node_id("r1")

    def test_a_trial_outcome_reaches_the_model_node_when_there_is_one(
        self, graph: Provenance
    ) -> None:
        sha = "a" * 64
        graph.add_node(MODEL, ts=T0, ts_end=T0, node_id=model_node_id("demo", 2, sha), payload={})

        outcome = record_outcome(
            graph,
            name="demo",
            shadow_version=2,
            active_version=1,
            since=T0,
            until=T0 + 60,
            ended=CLEARED,
            summary={"pairs": 0},
            by="dashboard",
            shadow_sha256=sha,
        )

        assert [(e.src, e.relation) for e in graph.edges(outcome.id)] == [
            (model_node_id("demo", 2, sha), EVALUATED_BY)
        ]

    def test_without_a_model_node_the_outcome_stands_alone(self, graph: Provenance) -> None:
        outcome = record_outcome(
            graph,
            name="demo",
            shadow_version=2,
            active_version=1,
            since=T0,
            until=T0 + 60,
            ended=CLEARED,
            summary={},
            by="",
            shadow_sha256="b" * 64,
        )

        assert graph.edges(outcome.id) == []


class TestTheExampleRecipe:
    """The IMU example's recipe, loaded by its file, imports the model beside it."""

    def test_it_fits_normal_readings_and_scores_the_faults_caught(self) -> None:
        recipe = load_recipe("examples/imu_anomaly/recipe.py")
        rng = np.random.default_rng(0)
        normal = rng.normal((0.0, 0.0, 1.0), (0.3, 0.3, 0.2), size=(200, 3))
        jolts = rng.normal((0.0, 0.0, 1.0), 3.0, size=(20, 3))
        rows = np.vstack([normal, jolts])
        frame = pa.table(
            {
                "ts": [T0 + i for i in range(len(rows))],
                "ax": rows[:, 0],
                "ay": rows[:, 1],
                "az": rows[:, 2],
                "label": [None] * 200 + ["fault"] * 20,
            }
        )

        model = recipe.fit(frame, {})
        figures = recipe.metrics(model, frame)

        assert set(model) == {"mean", "inverse_covariance"}
        assert figures["labelled_faults"] == 20.0
        assert figures["caught"] > 0.5
        assert figures["false_alarms"] < 0.1

    def test_without_labels_every_reading_is_normal(self) -> None:
        recipe = load_recipe("examples/imu_anomaly/recipe.py")
        frame = pa.table(
            {
                "ts": [T0, T0 + 1, T0 + 2],
                "ax": [0.1, 0.2, 0.3],
                "ay": [0.0, 0.1, 0.0],
                "az": [1.0, 1.1, 0.9],
            }
        )

        figures = recipe.metrics(recipe.fit(frame, {}), frame)

        assert "caught" not in figures and figures["labelled_faults"] == 0.0
