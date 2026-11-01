"""A library agent on a node has its models, and answers as it does on main.

A function declared with ``@wactorz.agent`` names its models in its code, which
main has and the node builds from its own install. Main sends them with the
spawn as artifacts, so the node fetches them before it builds the agent, and
the agent finds them there rather than in a store the node does not have. An
`Actor` subclass is built the same way and answers a task through its mailbox.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.test_node_runner import RecordingRunner
from tests.test_spawn_remote import _Main as SpawningMain
from wactorz import plugins
from wactorz.agents.function_agent import FunctionAgent, agent
from wactorz.agents.main import artifact_service as service_module
from wactorz.agents.main.artifact_service import ArtifactService
from wactorz.core.actor import Actor, Message, MessageType
from wactorz.core.artifacts import ArtifactStore, pinned
from wactorz.node.hosted import NodeHosted


@agent(subscribes="imu/in", publishes="imu/out", models={"model": "imu-detector"})
async def scorer(reading: dict, me: FunctionAgent) -> dict:
    """Reads the model the spawn brought, by the key the decorator gives it."""
    model = await me.load_model("model")
    return {"distance": float(abs(reading["az"] - model["mean"][2]))}


class Echo(Actor):
    """A library agent written as a class: it answers a task in its mailbox."""

    async def handle_message(self, msg: Message) -> None:
        if msg.type is not MessageType.TASK or not isinstance(msg.payload, dict):
            return
        reply = {"echo": msg.payload.get("x"), "_task_id": msg.payload.get("_task_id")}
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, reply)


@pytest.fixture(name="targets")
def targets_fixture() -> Iterator[dict[str, str]]:
    with plugins.registered([scorer, Echo]) as registered:
        yield {plugin.name: plugin.target for plugin in registered}


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """Main's store, where the artifact service looks for it."""
    state = tmp_path / "main-state"
    monkeypatch.setattr(service_module, "resolve_state_dir", lambda: str(state))
    return ArtifactStore.in_state_dir(state)


@pytest.fixture(name="stored")
def stored_fixture(store: ArtifactStore, tmp_path: Path) -> dict[str, Any]:
    """``imu-detector:1``, an npz of the arrays the scorer reads, pinned."""
    path = tmp_path / "imu.npz"
    np.savez(path, mean=np.array([0.0, 0.0, 9.81]), inverse_covariance=np.eye(3))
    # As a spawn naming ``imu-detector`` pins it: a bare name follows ``active``.
    return {**pinned(store.add(path, "imu-detector")), "follows": "active"}


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    runner = RecordingRunner(tmp_path / "node-state")
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


class TestMainSendingOne:
    @pytest.fixture(name="main")
    def main_fixture(self, store: ArtifactStore, targets: dict[str, str]) -> SpawningMain:
        main = SpawningMain()
        main.actor.artifact_service = ArtifactService(main.actor)
        main.actor.nodes.node_manifests["rpi"] = {"agents": list(targets.values())}
        return main

    async def test_the_models_it_names_go_as_artifacts_pinned(
        self, main: SpawningMain, stored: dict[str, Any], targets: dict[str, str]
    ) -> None:
        await main.spawn({"name": "scorer", "type": "module", "target": targets["scorer"]})

        _, sent, _ = main.published_to("/spawn")
        assert sent["artifacts"] == {"model": stored}

    async def test_artifacts_the_spawn_names_itself_are_kept(
        self, main: SpawningMain, stored: dict[str, Any], targets: dict[str, str]
    ) -> None:
        config = {
            "name": "scorer",
            "type": "module",
            "target": targets["scorer"],
            "artifacts": {"model": "imu-detector:1"},
        }

        await main.spawn(config)

        _, sent, _ = main.published_to("/spawn")
        # Pinned to the version it named, so it follows no pointer.
        assert sent["artifacts"] == {"model": {k: v for k, v in stored.items() if k != "follows"}}

    async def test_one_naming_no_models_sends_none(
        self, main: SpawningMain, targets: dict[str, str]
    ) -> None:
        await main.spawn({"name": "echo", "type": "module", "target": targets["echo"]})

        _, sent, _ = main.published_to("/spawn")
        assert "artifacts" not in sent

    def test_a_plugin_says_what_it_names(self, targets: dict[str, str]) -> None:
        assert plugins.for_target(targets["scorer"]).models == {"model": "imu-detector"}  # pyright: ignore[reportOptionalMemberAccess]
        assert plugins.for_target(targets["echo"]).models == {}  # pyright: ignore[reportOptionalMemberAccess]


class TestTheNodeRunningOne:
    async def test_it_loads_the_model_the_spawn_brought(
        self,
        runner: RecordingRunner,
        stored: dict[str, Any],
        targets: dict[str, str],
        tmp_path: Path,
    ) -> None:
        # What the node fetched before it built the agent, already in its cache.
        runner.artifacts.directory.mkdir(parents=True)
        runner.artifacts.path(stored["sha256"]).write_bytes((tmp_path / "imu.npz").read_bytes())

        await runner.spawn_agent(
            {
                "name": "scorer",
                "type": "module",
                "target": targets["scorer"],
                "artifacts": {"model": stored},
            }
        )

        hosted = runner.hosted("scorer")
        assert isinstance(hosted, NodeHosted)
        assert hosted.artifacts["model"]["path"] == str(runner.artifacts.path(stored["sha256"]))
        await runner._run_task(hosted, {"az": 10.81}, "reply/here")
        (answer,) = [p for t, p, _r in runner.published if t == "reply/here"]
        assert answer == {"distance": pytest.approx(1.0)}

    async def test_one_whose_model_never_came_does_not_start(
        self, runner: RecordingRunner, targets: dict[str, str]
    ) -> None:
        # No artifacts in the spawn, and no store on the node to find them in.
        await runner.spawn_agent(
            {
                "name": "scorer",
                "type": "module",
                "target": targets["scorer"],
                "_migration_token": "t",
            }
        )

        (ack,) = [p for t, p, _r in runner.published if t.endswith("/spawn_ack")]
        assert "refused" in ack

    async def test_a_class_answers_a_task_through_its_mailbox(
        self, runner: RecordingRunner, targets: dict[str, str]
    ) -> None:
        await runner.spawn_agent({"name": "echo", "type": "module", "target": targets["echo"]})
        hosted = runner.hosted("echo")
        assert isinstance(hosted, NodeHosted) and isinstance(hosted, Echo)

        await runner._run_task(hosted, {"x": 5}, "reply/here")

        (answer,) = [p for t, p, _r in runner.published if t == "reply/here"]
        assert answer["echo"] == 5

    async def test_a_plain_value_comes_back_as_a_result(
        self, runner: RecordingRunner, targets: dict[str, str]
    ) -> None:
        with plugins.registered([plain]) as (plugin,):
            await runner.spawn_agent({"name": "plain", "type": "module", "target": plugin.target})
            hosted = runner.hosted("plain")
            assert hosted is not None

            assert await hosted.run_task({"x": 2}) == {"result": 4}


@agent(description="Doubles what it is sent.")
def plain(payload: dict) -> int:
    return payload["x"] * 2
