"""Running a one-shot plan: the agents it needs, each step, and the answer.

A step names an agent. If the plan also carries a spawn config and that agent is
not running, it is spawned first; if the spawn fails the step falls back to
main rather than being dropped. An agent that only reacts to a stream is marked
spawn-only, because a TASK sent to it would wait out its timeout for a reply
that never comes.

`tests/test_planner_async_paths.py` covers dependency ordering. This covers
what a single step does and how the results become one answer, plus the two
places the planner asks other agents for context: Home Assistant's entity list
and one live payload per topic.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner import context as context_mod
from wactorz.agents.planner import execution as execution_mod
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.context import (
    collect_one_payload_per_topic,
    describe_samples,
    topics_worth_sampling,
)
from wactorz.core.actor import MessageType
from wactorz.core.topic_bus import TopicBus, TopicContract


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"


class _Registry:
    def __init__(self, *names: str) -> None:
        self._actors = [_Actor(n) for n in names]

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors if a.name == name), None)


class _Llm:
    async def complete(self, **_kwargs: Any) -> Any:
        raise RuntimeError("model overloaded")


def _planner(tmp_path: Path, script: dict[str, str] | None = None) -> PlannerAgent:
    return PlannerAgent(
        llm_provider=FakeProvider(script=script or {}),
        persistence_dir=str(tmp_path),
        auto_terminate=False,
    )


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path) -> PlannerAgent:
    return _planner(tmp_path)


@pytest.fixture(name="instant_sleep")
def instant_sleep_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settle pause after a spawn is for a real agent starting; none starts here."""
    real_sleep = asyncio.sleep

    async def _instant(_delay: float, *args: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr(execution_mod.asyncio, "sleep", _instant)


def _answer_with(planner: PlannerAgent, reply: Any) -> list[tuple[str, dict[str, Any]]]:
    """Make `send` resolve the pending future with `reply`, recording what went out."""
    sent: list[tuple[str, dict[str, Any]]] = []

    async def _send(target: str, msg_type: MessageType, payload: dict[str, Any]) -> bool:
        sent.append((target, payload))
        future = planner._result_futures[payload["_task_id"]]
        if isinstance(reply, Exception):
            future.set_exception(reply)
        elif reply is not None:
            future.set_result(reply)
        return True

    planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]
    return sent


def _spawns(planner: PlannerAgent, result: Any = "actor") -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []

    async def _spawn(config: dict[str, Any]) -> Any:
        configs.append(config)
        if isinstance(result, Exception):
            raise result
        return result

    planner._spawn_agent = _spawn  # pyright: ignore[reportAttributeAccessIssue]
    return configs


class TestEnsureAgents:
    async def test_without_a_registry_the_plan_is_unchanged(self, planner: PlannerAgent) -> None:
        plan = [{"step": 1, "agent": "x", "spawn_config": {"name": "x"}}]

        assert await planner._ensure_agents(plan) == plan

    async def test_steps_without_a_spawn_config_or_name_are_left_alone(
        self, planner: PlannerAgent
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        configs = _spawns(planner)
        plan = [{"step": 1, "agent": "news"}, {"step": 2, "spawn_config": {"type": "dynamic"}}]

        await planner._ensure_agents(plan)

        assert configs == []

    async def test_an_agent_already_running_is_used_as_is(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry("news")  # pyright: ignore[reportAttributeAccessIssue]
        configs = _spawns(planner)
        plan = [{"step": 1, "agent": "old", "spawn_config": {"name": "news"}}]

        await planner._ensure_agents(plan)

        assert configs == []
        assert plan[0]["agent"] == "news"

    @pytest.mark.parametrize(
        ("config", "spawn_only"),
        [
            ({"type": "dynamic", "code": "async def process(agent): ..."}, True),
            ({"type": "dynamic", "code": "agent.subscribe('t', cb)"}, True),
            ({"type": "dynamic", "code": "w = agent.window('t')"}, True),
            (
                {
                    "type": "dynamic",
                    "code": "agent.subscribe('t', cb)\nasync def handle_task(agent, p): ...",
                },
                False,
            ),
            ({"type": "dynamic", "code": "async def handle_task(agent, p): ..."}, False),
            ({"type": "llm", "code": "def process("}, False),
            ({"type": "dynamic", "code": "async def handle_task(): ...", "continuous": True}, True),
            ({"type": "dynamic", "code": "def process(", "continuous": False}, False),
        ],
    )
    async def test_a_spawned_stream_agent_is_marked_spawn_only(
        self,
        planner: PlannerAgent,
        instant_sleep: None,
        config: dict[str, Any],
        spawn_only: bool,
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _spawns(planner)
        plan = [{"step": 1, "agent": "watcher", "spawn_config": config}]

        await planner._ensure_agents(plan)

        assert plan[0].get("_spawn_only", False) is spawn_only
        assert planner._spawned_by_planner == ["watcher"]

    @pytest.mark.parametrize("result", [None, RuntimeError("no module named cv2")])
    async def test_a_failed_spawn_falls_back_to_main(
        self, planner: PlannerAgent, result: Any
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _spawns(planner, result=result)
        plan = [{"step": 1, "agent": "watcher", "spawn_config": {"type": "dynamic"}}]

        await planner._ensure_agents(plan)

        assert plan[0]["agent"] == "main"
        assert planner._spawned_by_planner == []

    async def test_a_spawn_goes_through_the_blocking_install_path(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []

        async def _spawn_local(config: dict[str, Any], **kwargs: Any) -> str:
            calls.append(kwargs)
            return "actor"

        monkeypatch.setattr(planner, "_spawn_local_from_config", _spawn_local)

        assert await planner._spawn_agent({"name": "x"}) == "actor"
        assert calls == [{"register": True, "blocking_install": True}]


class TestExecuteStep:
    async def test_a_spawn_only_step_reports_the_spawn_without_delegating(
        self, planner: PlannerAgent
    ) -> None:
        result = await planner._execute_step({"agent": "watcher", "_spawn_only": True}, {})

        assert result == {
            "result": "Agent 'watcher' spawned and running continuously.",
            "spawned": True,
        }

    async def test_a_step_for_main_is_answered_by_the_model_with_prior_context(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path, script={"summarise": "the summary"})
        prior: dict[str | int, Any] = {
            1: {"text": "headline one"},
            2: {"answer": "headline two"},
            3: {"other": 1},
        }

        result = await planner._execute_step(
            {"agent": "main", "task": "summarise", "depends_on": [1, 2, 3]}, prior
        )

        assert result == {"result": "the summary"}
        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert "[Step 1 result]: headline one" in prompt
        assert "[Step 2 result]: headline two" in prompt
        assert "[Step 3 result]: {'other': 1}" in prompt

    async def test_a_step_for_another_agent_is_delegated(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry("news")  # pyright: ignore[reportAttributeAccessIssue]
        sent = _answer_with(planner, {"result": "headlines"})

        result = await planner._execute_step({"agent": "news", "task": "get news"}, {})

        assert result == {"result": "headlines"}
        ((target, payload),) = sent
        assert target == "id-news"
        assert payload["text"] == "get news"
        assert payload["_reply_to"] == planner.actor_id
        assert planner._result_futures == {}

    async def test_no_response_is_an_error(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(planner, "_delegate", _nothing)

        result = await planner._execute_step({"agent": "news", "task": "t"}, {})

        assert result == {"error": "No response from news"}

    async def test_an_agent_that_crashed_is_answered_by_main_instead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = _planner(tmp_path, script={"failed": "best effort answer"})

        async def _crashed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"error": "KeyError: 'temp'", "error_phase": "handle_task"}

        monkeypatch.setattr(planner, "_delegate", _crashed)

        result = await planner._execute_step({"agent": "news", "task": "get news"}, {})

        assert result == {
            "result": "best effort answer",
            "fallback": True,
            "original_error": "KeyError: 'temp'",
        }


class TestDelegation:
    async def test_without_a_registry_there_is_no_answer(self, planner: PlannerAgent) -> None:
        assert await planner._delegate("news", "t") is None

    async def test_an_unknown_agent_is_an_error(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._delegate("ghost", "t") == {"error": "Agent 'ghost' not found"}

    async def test_a_silent_agent_times_out_and_is_forgotten(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry("news")  # pyright: ignore[reportAttributeAccessIssue]
        _answer_with(planner, None)

        result = await planner._delegate_with_payload("news", {"x": 1}, timeout=0.01)

        assert result == {"error": "Timeout from news"}
        assert planner._result_futures == {}


class TestSynthesize:
    PLAN: list[dict[str, Any]] = [  # noqa: RUF012  # read-only fixture
        {"step": 1, "agent": "news", "task": "get news"},
        {"step": 2, "agent": "weather", "task": "get weather"},
    ]

    async def test_only_spawns_are_confirmed_without_asking_the_model(
        self, planner: PlannerAgent
    ) -> None:
        plan = [
            {"step": 1, "agent": "a", "spawn_config": {"description": "watches a"}},
            {"step": 2, "agent": "b", "task": "watch b"},
        ]
        results: dict[str | int, Any] = {1: {"spawned": True}, 2: {"spawned": True}}

        answer = await planner._synthesize("t", plan, results)

        assert answer.startswith("Done! Spawned 2 continuous agent(s):")
        assert "• **a** — watches a" in answer
        assert "• **b** — watch b" in answer
        assert isinstance(planner.llm, FakeProvider)
        assert planner.llm.calls == []

    async def test_without_a_model_the_results_are_listed(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=None, persistence_dir=str(tmp_path))
        results: dict[str | int, Any] = {1: {"result": "headlines"}, 2: {"error": "down"}}

        answer = await planner._synthesize("t", self.PLAN, results)

        assert answer == "[@news]: headlines\n\n[@weather]: {'error': 'down'}"

    async def test_the_model_writes_one_answer_from_every_result(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"ORIGINAL TASK": "combined answer"})
        results: dict[str | int, Any] = {1: {"text": "headlines"}, 2: {"answer": "sunny"}}

        answer = await planner._synthesize("brief me", self.PLAN, results)

        assert answer == "combined answer"
        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert "Step 1 (@news): headlines" in prompt
        assert "Step 2 (@weather): sunny" in prompt

    async def test_a_failing_model_returns_the_raw_results(self, planner: PlannerAgent) -> None:
        planner.llm = _Llm()  # pyright: ignore[reportAttributeAccessIssue]
        results: dict[str | int, Any] = {1: {"result": "headlines"}, 2: {"result": "sunny"}}

        answer = await planner._synthesize("t", self.PLAN, results)

        assert answer == "Step 1 (@news): headlines\n\nStep 2 (@weather): sunny"


class TestFetchHaEntities:
    async def test_without_a_registry_or_ha_agent_there_are_none(
        self, planner: PlannerAgent
    ) -> None:
        assert await planner._fetch_ha_entities() == []
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        assert await planner._fetch_ha_entities() == []

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ({"entities": [{"entity_id": "light.a"}]}, [{"entity_id": "light.a"}]),
            ({"result": [{"entity_id": "light.b"}]}, [{"entity_id": "light.b"}]),
            ({"devices": [{"entities": []}]}, [{"entities": []}]),
            ({"result": "no entities"}, []),
            (RuntimeError("HA agent crashed"), []),
        ],
    )
    async def test_every_reply_shape_is_read(
        self, planner: PlannerAgent, reply: Any, expected: list[Any]
    ) -> None:
        planner._registry = _Registry("home-assistant-agent")  # pyright: ignore[reportAttributeAccessIssue]
        sent = _answer_with(planner, reply)

        assert await planner._fetch_ha_entities() == expected
        assert sent[0][1]["text"] == "list entities"
        assert planner._result_futures == {}


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Client":
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        for message in self._messages:
            yield message


class TestLiveTopicSampling:
    @staticmethod
    def _bus(*contracts: TopicContract) -> TopicBus:
        bus = TopicBus()
        for contract in contracts:
            bus.register_contract(contract)
        return bus

    def test_five_topics_per_agent_are_taken_until_ten_are_reached(self) -> None:
        bus = self._bus(
            TopicContract(name="a", publishes=[f"a/{i}" for i in range(7)]),
            TopicContract(name="b", publishes=[f"b/{i}" for i in range(7)]),
            TopicContract(name="c", publishes=["c/0"]),
        )

        topics = topics_worth_sampling(bus)

        assert topics == [(f"a/{i}", "a") for i in range(5)] + [(f"b/{i}", "b") for i in range(5)]

    def test_a_topic_published_by_two_agents_is_sampled_once(self) -> None:
        bus = self._bus(
            TopicContract(name="a", publishes=["shared"]),
            TopicContract(name="b", publishes=["shared", "b/0"]),
        )

        assert topics_worth_sampling(bus) == [("shared", "a"), ("b/0", "b")]

    async def test_the_first_dict_payload_per_topic_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client(
            [
                _Message("t/1", b"not json"),
                _Message("t/1", b'{"temp": 20}'),
                _Message("t/1", b'{"temp": 99}'),
                _Message("t/2", b"[1, 2]"),
            ]
        )
        monkeypatch.setattr(context_mod, "mqtt_client", client)

        received = await collect_one_payload_per_topic(
            "broker", 1883, [("t/1", "a"), ("t/2", "b")], "planner"
        )

        assert client.subscribed == ["t/1", "t/2"]
        assert received == {"t/1": {"temp": 20}}

    async def test_it_stops_once_every_topic_has_a_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client([_Message("t/1", b'{"a": 1}'), _Message("t/1", b'{"a": 2}')])
        monkeypatch.setattr(context_mod, "mqtt_client", client)

        received = await collect_one_payload_per_topic("broker", 1883, [("t/1", "a")], "planner")

        assert received == {"t/1": {"a": 1}}

    async def test_a_broker_that_refuses_yields_nothing(self) -> None:
        # The suite-wide fixture refuses every broker connection.
        assert await collect_one_payload_per_topic("broker", 1883, [("t", "a")], "p") == {}

    def test_samples_are_described_and_recorded_on_their_contract(self) -> None:
        contract = TopicContract(name="thermo", publishes=["t/1"])
        bus = self._bus(contract, TopicContract(name="other", publishes=["x"]))

        lines = describe_samples(bus, {"t/1": {"temp": 20.5, "_id": 3}}, {"t/1": "thermo"})

        assert lines == [
            "  Topic: t/1  (published by thermo)\n"
            "    Fields: {'temp': 'float'}\n"
            "    Example payload: {'temp': 20.5, '_id': 3}"
        ]
        assert contract.observed_samples["t/1"]["fields"] == {"temp": "float"}

    async def test_the_planner_samples_through_its_own_broker(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = self._bus(TopicContract(name="thermo", publishes=["t/1"]))
        monkeypatch.setattr(
            context_mod, "mqtt_client", _Client([_Message("t/1", json.dumps({"t": 1}).encode())])
        )

        lines = await planner._sample_live_topics(bus)

        assert len(lines) == 1
        assert await planner._sample_live_topics(TopicBus()) == []
