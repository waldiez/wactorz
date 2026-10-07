"""A developer's registered agent is a building block for main and the planner.

Declared with ``@wactorz.agent`` and registered, an agent is something chat
can ask, main can start, and the planner places as a pipeline step, with
nothing more declared than the decorator says. The IMU example is the agent
under test, with a small model trained in the test.
"""

from __future__ import annotations

import importlib
import json
import pickle
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wactorz import plugins
from wactorz.agents.function_agent import AgentSpec, agent, spec_of
from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.main.actor import MainActor
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.pipeline import registered_agents_section
from wactorz.core.actor import Actor, ActorState
from wactorz.web import chat, runtime

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    plugins.clear()
    yield
    plugins.clear()


@pytest.fixture(autouse=True)
def _restore_runtime() -> Iterator[None]:
    registry = runtime.registry
    yield
    runtime.registry = registry


@pytest.fixture(name="imu")
def imu_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, Path]:
    """The IMU example's agent module, and a model trained on resting readings.

    The example is a folder of plain files that import each other by name, so
    it is imported the way its own scripts are: with its folder on the path.
    """
    monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
    for name in ("model", "agent"):
        sys.modules.pop(name, None)
    model_module = importlib.import_module("model")
    module = importlib.import_module("agent")

    rng = np.random.default_rng(0)
    resting = rng.normal(loc=(0.0, 0.0, 9.8), scale=0.05, size=(500, 3))
    model_path = tmp_path / "imu_model.pkl"
    model_path.write_bytes(pickle.dumps(model_module.MahalanobisModel.fit(resting)))
    return module, model_path


def _spec(fn: Any) -> AgentSpec:
    spec = spec_of(fn)
    assert spec is not None
    return spec


class _Registry:
    """A registry of running actors, by name, the way main and the chat see one."""

    def __init__(self, *actors: Actor) -> None:
        self._actors = list(actors)

    def all_actors(self) -> list[Actor]:
        return list(self._actors)

    def find_by_name(self, name: str) -> Actor | None:
        return next((a for a in self._actors if a.name == name), None)

    def get(self, actor_id: str) -> Actor | None:
        return next((a for a in self._actors if a.actor_id == actor_id), None)


@agent(
    name="probe",
    subscribes="in/x",
    publishes="out/x",
    description="Echoes a reading.",
    input_schema={"value": "float"},
    output_schema={"value": "float"},
)
def probe(reading: dict) -> dict:
    return reading


class TestTheSection:
    def test_nothing_registered_is_no_section(self) -> None:
        assert registered_agents_section({}, set()) == ""

    def test_each_agent_is_described_with_its_one_spawn_config(self) -> None:
        plugin = plugins.register(probe)
        section = registered_agents_section(plugins.discover(), set())

        assert "═══ REGISTERED AGENTS" in section
        assert "probe — Echoes a reading.  [NOT running]" in section
        assert "subscribes: in/x" in section
        assert "publishes:  out/x" in section
        assert '"input":' not in section  # schemas are shown as JSON values, not nested
        assert 'input:  {"value": "float"}' in section
        spawn_config = {"name": "probe", "type": "module", "target": plugin.target}
        assert f"spawn_config: {json.dumps(spawn_config)}" in section
        assert "Never invent or alter a target" in section

    def test_a_running_agent_is_marked_so(self) -> None:
        plugins.register(probe)
        assert "probe — Echoes a reading.  [running]" in registered_agents_section(
            plugins.discover(), {"probe"}
        )

    def test_an_agent_registered_without_a_target_has_no_spawn_config(self) -> None:
        plugin = plugins.register(probe)
        plugin.target = ""
        section = registered_agents_section(plugins.discover(), set())
        assert "spawn_config: none" in section
        assert '"type": "module"' not in section.split("RULES FOR REGISTERED AGENTS")[0]


class TestThePlanner:
    """The section reaches the model, and a module step for a registered agent
    survives the planner's own validation."""

    @staticmethod
    def _planner(tmp_path: Path, plan: list[dict[str, Any]], registry: Any) -> PlannerAgent:
        provider = FakeProvider(script={"USER REQUEST": json.dumps(plan)})
        planner = PlannerAgent(
            llm_provider=provider,
            persistence_dir=str(tmp_path),
            auto_terminate=False,
            prompt_fragments=(),
        )
        planner._registry = registry  # pyright: ignore[reportAttributeAccessIssue]

        async def _bus() -> tuple[str, str]:
            return "", ""

        async def _urls(_task: str) -> str:
            return ""

        planner._gather_topic_bus_context = _bus  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_notification_urls = _urls  # pyright: ignore[reportAttributeAccessIssue]
        return planner

    async def test_a_registered_detector_is_proposed_as_the_first_step(
        self, tmp_path: Path, imu: tuple[Any, Path]
    ) -> None:
        module, _ = imu
        plugin = plugins.register(module.detect)
        detector = {"name": "imu-anomaly", "type": "module", "target": plugin.target}
        plan = [
            {
                "name": "imu-anomaly",
                "description": "the registered detector",
                "spawn_config": detector,
            },
            {
                "name": "imu-alert",
                "description": "alerts above 20",
                "spawn_config": {"type": "dynamic", "code": "async def setup(agent):\n    pass\n"},
            },
        ]
        planner = self._planner(tmp_path, plan, _Registry())

        proposed = await planner._decompose_pipeline(
            "alert me when the IMU detector scores above 20", []
        )

        assert proposed[0]["spawn_config"] == detector
        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert "═══ REGISTERED AGENTS" in prompt
        assert (
            "imu-anomaly — Flags IMU readings the trained model calls abnormal.  [NOT running]"
            in prompt
        )
        assert "publishes:  anomalies/imu" in prompt
        assert f'"target": "{plugin.target}"' in prompt
        assert "═══ NOTIFICATION URLS ═══" in prompt

    async def test_a_running_detector_is_shown_as_a_live_source(
        self, tmp_path: Path, imu: tuple[Any, Path]
    ) -> None:
        module, model_path = imu
        plugins.register(module.detect)
        running = _spec(module.detect).build(
            persistence_dir=str(tmp_path), options={"model": str(model_path)}
        )
        planner = self._planner(tmp_path, [], _Registry(running))

        await planner._decompose_pipeline("notify me on anomalies", [])

        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert (
            "imu-anomaly — Flags IMU readings the trained model calls abnormal.  [running]"
            in prompt
        )

    async def test_without_registered_agents_the_prompt_has_no_such_section(
        self, tmp_path: Path
    ) -> None:
        planner = self._planner(tmp_path, [], _Registry())
        await planner._decompose_pipeline("notify me", [])
        assert isinstance(planner.llm, FakeProvider)
        assert "REGISTERED AGENTS" not in planner.llm.calls[-1][1][-1]["content"]


class TestMain:
    def test_a_registered_agent_that_is_not_running_is_listed_as_startable(
        self, tmp_path: Path
    ) -> None:
        plugin = plugins.register(probe)
        main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
        main._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]

        main._rebuild_system_prompt()

        assert "== REGISTERED BUT NOT RUNNING" in main.system_prompt
        assert "  probe — Echoes a reading." in main.system_prompt
        spawn_config = {"name": "probe", "type": "module", "target": plugin.target}
        assert f"spawn with: {json.dumps(spawn_config)}" in main.system_prompt

    def test_once_it_runs_it_is_in_the_running_list_instead(self, tmp_path: Path) -> None:
        plugins.register(probe)
        running = _spec(probe).build(persistence_dir=str(tmp_path))
        main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
        main._registry = _Registry(running)  # pyright: ignore[reportAttributeAccessIssue]

        main._rebuild_system_prompt()

        assert "== REGISTERED BUT NOT RUNNING" not in main.system_prompt
        assert "  probe — Echoes a reading." in main.system_prompt

    def test_without_a_registry_nothing_is_claimed(self, tmp_path: Path) -> None:
        """Main built alone cannot tell running from not; the pinned prompt
        depends on it saying nothing."""
        plugins.register(probe)
        main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
        main._rebuild_system_prompt()
        assert "REGISTERED BUT NOT RUNNING" not in main.system_prompt


class TestFromChat:
    @staticmethod
    async def _say(text: str) -> list[str]:
        replies: list[str] = []

        async def _reply(message: str) -> None:
            replies.append(message)

        await chat.route_chat(text, _reply)
        return replies

    async def test_the_detector_answers_a_json_reading_with_its_score(
        self, tmp_path: Path, imu: tuple[Any, Path]
    ) -> None:
        module, model_path = imu
        detector = _spec(module.detect).build(
            persistence_dir=str(tmp_path), options={"model": str(model_path)}
        )
        detector.state = ActorState.RUNNING
        runtime.registry = _Registry(detector)

        replies = await self._say('@imu-anomaly {"ax": 9, "ay": 0, "az": 1}')

        assert len(replies) == 1
        answer = json.loads(replies[0])
        assert answer["score"] > 20
        assert answer["reading"] == {"ax": 9, "ay": 0, "az": 1}

    async def test_a_resting_reading_is_not_an_anomaly(
        self, tmp_path: Path, imu: tuple[Any, Path]
    ) -> None:
        module, model_path = imu
        detector = _spec(module.detect).build(
            persistence_dir=str(tmp_path), options={"model": str(model_path)}
        )
        detector.state = ActorState.RUNNING
        runtime.registry = _Registry(detector)

        replies = await self._say('@imu-anomaly {"ax": 0, "ay": 0, "az": 9.8}')

        assert len(replies) == 1
        assert json.loads(replies[0]) == {"result": None}

    def test_a_structured_reply_is_shown_as_json(self) -> None:
        """A function's return value is data; shown as JSON it can be read and
        pasted on, where a Python repr can be neither."""
        assert chat.reply_text({"score": 12.4, "reading": {"ax": 9}}) == (
            '{"score": 12.4, "reading": {"ax": 9}}'
        )
        assert chat.reply_text({"result": "done"}) == "done"

    def test_a_json_body_is_the_payload_and_text_is_text(self) -> None:
        assert chat.task_payload('{"ax": 9, "ay": 0}') == {"ax": 9, "ay": 0}
        assert chat.task_payload('  {"a": 1}  ') == {"a": 1}
        assert chat.task_payload("[1, 2]") == {"text": "[1, 2]"}
        assert chat.task_payload('{"not json"') == {"text": '{"not json"'}
        assert chat.task_payload("is it raining?") == {"text": "is it raining?"}
