"""A subscribe callback that keeps failing is repaired in place, memory intact.

The same path a crashing process() takes: at three straight failures the model
is asked for a fix, the old program is torn down, and the repaired one starts
from its own setup() on the same agent -- persisted keys and all. The agent is
never restarted by the Supervisor for it, so nothing it remembered is lost.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import listener as listener_module
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import ActorState

TOPIC = "camera/livingroom/person"

BROKEN = """
async def setup(agent):
    def on_person(payload):
        total = agent.recall("person_total", 0) + 1
        agent.persist("person_total", total)
        agent.state["seen_by"] = "broken"
        if total >= 10:
            1 / 0
    agent.subscribe("camera/livingroom/person", on_person)
"""

FIXED = """
async def setup(agent):
    def on_person(payload):
        total = agent.recall("person_total", 0) + 1
        agent.persist("person_total", total)
        agent.state["seen_by"] = "fixed"
    agent.subscribe("camera/livingroom/person", on_person)
"""


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    async def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)

    @property
    def messages(self) -> Any:
        async def _stream() -> Any:
            while True:
                yield await self._broker.queue.get()

        return _stream()


class FakeBroker:
    def __init__(self) -> None:
        self.connections: list[FakeClient] = []
        self.queue: asyncio.Queue = asyncio.Queue()

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> FakeClient:
        client = FakeClient(self)
        self.connections.append(client)
        return client

    async def deliver(self, topic: str = TOPIC, payload: bytes = b"{}") -> None:
        await self.queue.put(FakeMessage(topic, payload))


class ScriptedLLM:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, messages: Any, system: str = "", max_tokens: int = 0) -> Any:
        self.prompts.append(messages[-1]["content"])
        answer = self.answers.pop(0) if self.answers else FIXED
        return answer, {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(listener_module, "mqtt_client", fake)
    return fake


def make_agent(tmp_path: Path, code: str, llm: Any) -> DynamicAgent:
    agent = DynamicAgent(
        name="person-counter", code=code, llm_provider=llm, persistence_dir=str(tmp_path)
    )
    agent.state = ActorState.RUNNING
    return agent


async def settle(rounds: int = 12) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0)


async def shut_down(agent: DynamicAgent) -> None:
    agent.state = ActorState.STOPPED
    for task in agent._tasks:
        task.cancel()
    await asyncio.gather(*agent._tasks, return_exceptions=True)


async def run_detections(agent: DynamicAgent, broker: FakeBroker, count: int) -> None:
    for _ in range(count):
        await broker.deliver()
        await settle()


class TestRepairingASubscribeCallback:
    async def test_the_third_failure_repairs_the_program_and_the_total_carries_on(
        self, tmp_path: Path, broker: FakeBroker
    ) -> None:
        llm = ScriptedLLM(FIXED)
        agent = make_agent(tmp_path, BROKEN, llm)
        await agent.on_start()
        await settle()

        # 9 clean detections, then the 10th, 11th and 12th fail.
        await run_detections(agent, broker, 12)
        await until(lambda: agent._code == FIXED.strip())
        await settle()
        # The repaired program handles the next ones.
        await run_detections(agent, broker, 3)
        await shut_down(agent)

        assert len(llm.prompts) == 1
        assert agent._api.state["seen_by"] == "fixed"
        # Memory: persisted under the same name, so the count never restarted.
        assert agent.recall("person_total") == 15
        assert agent.state is ActorState.RUNNING or agent.state is ActorState.STOPPED
        assert agent._cb_error_count.get(TOPIC) is None

    async def test_the_model_is_told_which_callback_and_topic_failed(
        self, tmp_path: Path, broker: FakeBroker
    ) -> None:
        llm = ScriptedLLM(FIXED)
        agent = make_agent(tmp_path, BROKEN, llm)
        await agent.on_start()
        await settle()

        await run_detections(agent, broker, 12)
        await until(lambda: len(llm.prompts) >= 1)
        await shut_down(agent)

        prompt = llm.prompts[0]
        assert f"subscribe callback on '{TOPIC}'" in prompt
        assert "division by zero" in prompt
        assert "KEEP THE AGENT'S MEMORY" in prompt

    async def test_the_old_callback_is_gone_after_the_repair(
        self, tmp_path: Path, broker: FakeBroker
    ) -> None:
        llm = ScriptedLLM(FIXED)
        agent = make_agent(tmp_path, BROKEN, llm)
        await agent.on_start()
        await settle()

        await run_detections(agent, broker, 12)
        await until(lambda: agent._code == FIXED.strip())
        await settle()
        errors_after_repair = agent.metrics.errors
        await run_detections(agent, broker, 5)
        await shut_down(agent)

        assert agent.metrics.errors == errors_after_repair
        hub = agent._sub_hub
        assert [b.topic for b in hub._bindings] == [TOPIC]
        assert len(hub._bindings) == 1, "the broken binding survived the repair"
        assert broker.connections[0].unsubscribed == [TOPIC]

    async def test_a_fix_that_does_not_compile_falls_back_to_failing_the_actor(
        self, tmp_path: Path, broker: FakeBroker
    ) -> None:
        # Two rounds are allowed; both come back broken, so the plain budget
        # runs on to FAILED and the Supervisor takes over.
        llm = ScriptedLLM("async def setup(agent)\n    pass\n", "def setup(agent)\n  x")
        agent = make_agent(tmp_path, BROKEN, llm)
        await agent.on_start()
        await settle()

        await run_detections(agent, broker, 9 + listener_module.CB_MAX_CONSECUTIVE_FAILURES)
        await until(lambda: agent.state is ActorState.FAILED)
        await shut_down(agent)

        assert len(llm.prompts) == 2
        assert agent._code == BROKEN
        assert agent.recall("person_total") == 9 + listener_module.CB_MAX_CONSECUTIVE_FAILURES

    async def test_without_a_model_the_budget_alone_applies(
        self, tmp_path: Path, broker: FakeBroker
    ) -> None:
        agent = make_agent(tmp_path, BROKEN, llm=None)
        await agent.on_start()
        await settle()

        await run_detections(agent, broker, 9 + listener_module.CB_MAX_CONSECUTIVE_FAILURES)
        await until(lambda: agent.state is ActorState.FAILED)
        await shut_down(agent)

        assert agent._cb_error_count[TOPIC] == listener_module.CB_MAX_CONSECUTIVE_FAILURES
