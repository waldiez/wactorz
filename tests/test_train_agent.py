"""The trainer runs one training on a task and answers when it ends; ``wactorz-train`` does the same from a terminal.

A library agent the package brings, spawned from the catalogue on main with
its recipe, model and source. A task runs a training over the agent's
options; a second task during one is refused; ``status`` answers at once; on
a node it refuses, since the store and the data are main's.
``docs/design/train-step.md`` is the decision these pin.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz import plugins
from wactorz.agents import train_agent
from wactorz.agents.train_agent import TrainAgent
from wactorz.core.actor import Message, MessageType
from wactorz.core.paths import resolve_state_dir
from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import ACTION, LED_TO, OBSERVATION, Provenance
from wactorz.data import data_root
from wactorz.data.parquet import Writer
from wactorz.monitoring import model_metrics
from wactorz.train import Outcome, TrainError, TrainSpec
from wactorz.train.cli import main
from wactorz.train.record import runs_of

T0 = 1_760_000_000.0
SOURCE = "sensors/x"

RECIPE = """
import numpy as np


def train(frame, settings):
    return {"mean": np.array([float(np.mean(frame.column("x").to_pylist()))])}


def score(model, frame):
    return {"error": float(abs(np.mean(frame.column("x").to_pylist()) - model["mean"][0]))}
"""


@pytest.fixture(name="recipe")
def recipe_fixture(tmp_path: Path) -> str:
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE)
    return str(path)


@pytest.fixture(name="recorded")
def recorded_fixture() -> Path:
    root = data_root(resolve_state_dir())
    writer = Writer(root, SOURCE)
    for i in range(60):
        writer.append(
            {"ts": T0 + i * 60, "arrived": T0 + i * 60, "time_source": "payload", "x": 1.0 + i / 60}
        )
    writer.close()
    return root


@pytest.fixture(name="feed")
def feed_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        if topic.endswith("/logs"):
            seen.append(payload)

    monkeypatch.setattr(TrainAgent, "_mqtt_publish", record)
    return seen


@pytest.fixture(name="sent")
def sent_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, Any]]:
    seen: list[tuple[str, Any, Any]] = []

    async def record(self: Any, target: str, kind: Any, payload: Any = None) -> bool:
        seen.append((target, kind, payload))
        return True

    monkeypatch.setattr(TrainAgent, "send", record)
    return seen


@pytest.fixture(name="trainer")
async def trainer_fixture(
    tmp_path: Path, recipe: str, feed: list[dict[str, Any]], sent: list[tuple[str, Any, Any]]
) -> AsyncIterator[TrainAgent]:
    agent = TrainAgent(
        name="trainer",
        persistence_dir=str(tmp_path / "agents"),
        recipe=recipe,
        model="demo",
        source=SOURCE,
        metric="error",
        higher_is_better=False,
        margin=0.01,
        settings={"a": 1},
        timeout_s=60.0,
    )
    yield agent


def _task(payload: dict[str, Any]) -> Message:
    return Message(type=MessageType.TASK, sender_id="main", payload=payload)


async def _settled(agent: TrainAgent) -> None:
    for _ in range(600):
        if agent.running is None and not any(not t.done() for t in agent._tasks):
            return
        await asyncio.sleep(0.05)


class TestTheSpecOfATask:
    def test_a_task_takes_the_agents_options_and_says_its_own(
        self, trainer: TrainAgent, recipe: str
    ) -> None:
        spec = trainer.spec_for(
            {
                "since": T0,
                "until": T0 + 3600,
                "settings": {"b": 2},
                "reason": "drift",
                "_task_id": "t",
            },
            now=T0 + 7200,
        )

        assert spec == TrainSpec.from_dict(
            {
                "recipe": recipe,
                "model": "demo",
                "source": SOURCE,
                "since": T0,
                "until": T0 + 3600,
                "settings": {"a": 1, "b": 2},
                "metric": "error",
                "margin": 0.01,
                "higher_is_better": False,
                "reason": "drift",
                "timeout_s": 60.0,
            }
        )

    def test_chat_text_is_the_run_as_json_or_the_reason(self, trainer: TrainAgent) -> None:
        as_json = trainer.spec_for({"text": ' {"since": 1, "until": 2, "reason": "drift"} '})
        as_words = trainer.spec_for({"text": "the valve was changed"}, now=T0)

        assert (as_json.since, as_json.until, as_json.reason) == (1.0, 2.0, "drift")
        assert (as_words.reason, as_words.until) == ("the valve was changed", T0)

    def test_the_window_is_the_last_week_when_unsaid(self, trainer: TrainAgent) -> None:
        spec = trainer.spec_for({}, now=T0)

        assert (spec.since, spec.until) == (T0 - 7 * 86400, T0)

    def test_what_cannot_be_a_run_is_refused(self, trainer: TrainAgent) -> None:
        with pytest.raises(TrainError, match="since < until"):
            trainer.spec_for({"since": T0, "until": T0 - 1})


class TestATask:
    async def test_a_run_is_answered_when_it_ends_and_said_on_the_feed(
        self,
        trainer: TrainAgent,
        recorded: Path,
        sent: list[tuple[str, Any, Any]],
        feed: list[dict[str, Any]],
    ) -> None:
        await trainer.handle_message(_task({"since": T0, "until": T0 + 3600, "_task_id": "t1"}))
        assert trainer.running is not None
        await _settled(trainer)

        (reply,) = [p for _, kind, p in sent if kind == MessageType.RESULT]
        assert (reply["ok"], reply["reference"], reply["_task_id"]) == (True, "demo:1", "t1")
        assert reply["moved"]["pointer"] == "shadow"
        assert trainer.last is not None and trainer.runs == 1
        assert [m["message"].split(",")[0] for m in feed] == [
            "training demo with " + trainer.options["recipe"] + " on sensors/x",
            "stored demo:1",
        ]

    async def test_a_run_is_joined_to_the_observation_the_task_names(
        self,
        trainer: TrainAgent,
        recorded: Path,
        sent: list[tuple[str, Any, Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with WactorzDB(tmp_path / "db" / "wactorz.db") as db:
            monkeypatch.setattr(train_agent, "get_db", lambda: db)
            graph = Provenance(db)
            alarm = graph.add_node(OBSERVATION, ts=T0, ts_end=T0 + 3600, source="models/demo")
            # As a rule's task action sends it: the alarm under ``trigger``.
            await trainer.handle_message(
                _task(
                    {
                        "since": T0,
                        "until": T0 + 3600,
                        "trigger": {"observation": alarm.id, "test": "psi"},
                        "_task_id": "t1",
                    }
                )
            )
            await _settled(trainer)

            (reply,) = [p for _, kind, p in sent if kind == MessageType.RESULT]
            assert reply["ok"] is True
            (run,) = runs_of(graph, "demo")
            assert (alarm.id, run["id"], LED_TO) in {
                (e.src, e.dst, e.relation) for e in graph.edges(run["id"])
            }
            assert next(n.id for n in graph.descendants(alarm.id)) == run["id"]
        assert model_metrics.TRAIN_RUNS.labels(model="demo", outcome="ok")._value.get() >= 1

    async def test_an_observation_the_graph_lacks_is_said_and_the_run_still_answers(
        self,
        trainer: TrainAgent,
        recorded: Path,
        sent: list[tuple[str, Any, Any]],
        feed: list[dict[str, Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with WactorzDB(tmp_path / "db" / "wactorz.db") as db:
            monkeypatch.setattr(train_agent, "get_db", lambda: db)
            await trainer.handle_message(
                _task({"since": T0, "until": T0 + 3600, "observation": "nowhere", "_task_id": "t1"})
            )
            await _settled(trainer)

        (reply,) = [p for _, kind, p in sent if kind == MessageType.RESULT]
        assert reply["ok"] is True
        assert any("not joined to nowhere" in m["message"] for m in feed)

    async def test_a_second_task_during_a_run_is_refused(
        self, trainer: TrainAgent, sent: list[tuple[str, Any, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(spec: TrainSpec, **_kwargs: Any) -> Outcome:
            started.set()
            await release.wait()
            return Outcome(run_id="r", run_dir=Path("r"), spec=spec, ok=True)

        monkeypatch.setattr(train_agent, "run", slow)
        await trainer.handle_message(_task({"_task_id": "first"}))
        await started.wait()

        await trainer.handle_message(_task({"_task_id": "second"}))

        assert [(p["_task_id"], p["error"]) for _, _, p in sent] == [
            ("second", "a run is in progress (starting); ask again when it has ended")
        ]
        release.set()
        await _settled(trainer)
        assert [p["_task_id"] for _, _, p in sent] == ["second", "first"]

    async def test_status_answers_at_once(
        self, trainer: TrainAgent, sent: list[tuple[str, Any, Any]]
    ) -> None:
        await trainer.handle_message(_task({"action": "status", "_task_id": "s"}))

        (reply,) = [p for _, _, p in sent]
        assert (reply["running"], reply["last"], reply["runs"], reply["options"]["model"]) == (
            None,
            None,
            0,
            "demo",
        )

    async def test_a_task_that_is_not_a_run_is_refused_saying_why(
        self, trainer: TrainAgent, sent: list[tuple[str, Any, Any]]
    ) -> None:
        await trainer.handle_message(_task({"split": "shuffle", "_task_id": "x"}))

        assert (
            sent[0][2]["error"]
            == "a split is one of holdout, time_split, rolling_origin, leave_k_out, not 'shuffle'"
        )

    async def test_on_a_node_every_run_is_refused(
        self, trainer: TrainAgent, sent: list[tuple[str, Any, Any]], feed: list[dict[str, Any]]
    ) -> None:
        trainer._runner = object()  # pyright: ignore[reportAttributeAccessIssue]  # the node runner's mark
        await trainer.on_start()
        await trainer.handle_message(_task({"_task_id": "n"}))

        assert (
            sent[-1][2]["error"]
            == "the trainer runs on main, beside the store and the data directory"
        )
        assert feed[0]["level"] == "warning"

    async def test_the_run_reaches_the_graph_through_mains_database(
        self, trainer: TrainAgent, recorded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with WactorzDB(tmp_path / "db" / "wactorz.db") as db:
            monkeypatch.setattr(train_agent, "get_db", lambda: db)
            outcome = await trainer.train(trainer.spec_for({"since": T0, "until": T0 + 3600}))

            assert outcome.ok
            (run,) = runs_of(Provenance(db), "demo")
            assert (run["by"], run["status"]) == ("agent:trainer", "done")
            assert Provenance(db).nodes(ACTION)[0].actor == "agent:trainer"


class TestTheCatalogue:
    def test_the_trainer_is_a_built_in_plugin_that_waits_to_be_asked_for(
        self, tmp_path: Path
    ) -> None:
        plugin = plugins.built_in()["trainer"]

        assert plugin.autostart is False
        assert {"recipe", "model", "source", "since", "split"} <= set(plugin.input_schema)
        built = plugin.build(
            name="trainer",
            persistence_dir=str(tmp_path),
            options={"recipe": "r.py", "model": "demo", "source": SOURCE, "margin": 0.1},
        )
        assert isinstance(built, TrainAgent)
        assert (built.options["recipe"], built.options["margin"]) == ("r.py", 0.1)


class TestTheCommand:
    @pytest.fixture(autouse=True)
    def _logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.train.cli")

    def test_check_says_what_a_recipe_defines(
        self, recipe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert main(["check", recipe]) == 0

        assert "train, score (save: a mapping of numpy arrays as .npz)" in caplog.text

    def test_check_says_why_a_recipe_will_not_load(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert main(["check", str(tmp_path / "none.py")]) == 1

        assert "cannot load the recipe" in caplog.text

    def test_run_trains_stores_and_writes_the_graph_beside_main(
        self, recipe: str, recorded: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state = resolve_state_dir()
        with WactorzDB(Path(state) / "wactorz.db"):
            pass

        code = main(
            [
                "run",
                recipe,
                "--model",
                "demo",
                "--source",
                SOURCE,
                "--since",
                str(T0),
                "--until",
                str(T0 + 3600),
                "--metric",
                "error",
                "--lower-is-better",
                "--split",
                "time:" + str(T0 + 2400),
                "--setting",
                "k=1",
                "--reason",
                "by hand",
                "--state-dir",
                state,
                "--json",
            ]
        )

        assert code == 0
        answer = json.loads(caplog.records[-1].getMessage())
        assert (answer["ok"], answer["reference"], answer["moved"]["pointer"]) == (
            True,
            "demo:1",
            "shadow",
        )
        assert answer["moved"]["by"] == "cli" and answer["moved"]["reason"].endswith("; by hand")
        with WactorzDB(Path(state) / "wactorz.db") as db:
            (run,) = runs_of(Provenance(db), "demo")
            assert run["split"] == {"kind": "time_split", "at": T0 + 2400} and run["settings"] == {
                "k": 1
            }

    def test_run_without_a_database_says_the_graph_was_not_written(
        self, recipe: str, recorded: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        code = main(
            [
                "run",
                recipe,
                "--model",
                "demo",
                "--source",
                SOURCE,
                "--set",
                "none",
                "--since",
                str(T0),
                "--until",
                str(T0 + 3600),
                "--state-dir",
                resolve_state_dir(),
            ]
        )

        assert code == 0
        assert "stored demo:1" in caplog.text
        assert "not in the provenance graph" in caplog.text

    def test_a_failed_run_is_an_error_exit(
        self, recipe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        code = main(
            [
                "run",
                recipe,
                "--model",
                "demo",
                "--source",
                SOURCE,
                "--metric",
                "error",
                "--since",
                str(T0),
                "--until",
                str(T0 + 60),
                "--state-dir",
                resolve_state_dir(),
            ]
        )

        assert code == 1
        assert "has no rows" in caplog.text

    def test_a_split_that_is_not_one_is_refused_by_the_parser(self, recipe: str) -> None:
        with pytest.raises(SystemExit):
            main(["run", recipe, "--model", "demo", "--source", SOURCE, "--split", "shuffle"])
