"""A function becomes an agent with one decorator, and stays a function.

`wactorz.agent` records a specification on the function and leaves it
callable. The actor built from it subscribes, publishes what the function
returns, answers tasks, and keeps the counters the dashboard shows.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

import wactorz
from wactorz.agents.function_agent import AgentSpec, FunctionAgent, agent, agent_name_from, spec_of
from wactorz.core.actor import Message, MessageType


class FakeRegistry:
    """Catches what an actor sends, so a task's reply can be read back."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, Message]] = []

    async def deliver(self, target_id: str, msg: Message) -> bool:
        self.delivered.append((target_id, msg))
        return True


@pytest.fixture(name="published")
def published_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every MQTT publish any function agent makes, by topic."""
    seen: list[tuple[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        seen.append((topic, payload))

    monkeypatch.setattr(FunctionAgent, "_mqtt_publish", record)
    return seen


def _spec(fn: Any) -> AgentSpec:
    spec = spec_of(fn)
    assert spec is not None
    return spec


def _build(fn: Any, tmp_path: Path, **kwargs: Any) -> FunctionAgent:
    return _spec(fn).build(persistence_dir=str(tmp_path), **kwargs)


class TestTheDecorator:
    def test_bare_use_names_the_agent_after_the_function(self) -> None:
        @agent
        def imu_anomaly(reading: dict) -> dict:
            """Flags odd readings."""
            return reading

        spec = spec_of(imu_anomaly)
        assert spec is not None
        assert spec.name == "imu-anomaly"
        assert spec.description == "Flags odd readings."
        assert spec.subscribes == ()
        # Still the function it was.
        assert imu_anomaly({"ax": 1}) == {"ax": 1}

    def test_arguments_are_recorded(self) -> None:
        @agent(
            name="detector",
            subscribes="sensors/imu/#",
            publishes="anomalies/imu",
            capabilities=["anomaly"],
            requires={"ram_mb": 256},
            autostart=False,
        )
        async def detect(reading: dict) -> dict | None:
            return None

        spec = spec_of(detect)
        assert spec == AgentSpec(
            fn=detect,
            name="detector",
            subscribes=("sensors/imu/#",),
            publishes="anomalies/imu",
            description="",
            capabilities=("anomaly",),
            requires={"ram_mb": 256},
            autostart=False,
        )

    def test_the_package_exports_it(self) -> None:
        assert wactorz.agent is agent

    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [("ImuAnomaly", "imu-anomaly"), ("detect_v2", "detect-v2"), ("HTTPProbe", "httpprobe")],
    )
    def test_names_are_topic_safe(self, identifier: str, expected: str) -> None:
        assert agent_name_from(identifier) == expected

    def test_a_function_may_take_the_actor_too(self) -> None:
        @agent
        def with_actor(reading: dict, me: Any) -> None:
            return None

        @agent
        def without(reading: dict) -> None:
            return None

        taking = spec_of(with_actor)
        plain = spec_of(without)
        assert taking is not None and plain is not None
        assert taking.wants_actor
        assert not plain.wants_actor

    def test_a_second_parameter_with_a_default_is_the_functions_own(self) -> None:
        @agent
        def detect(reading: dict, threshold: float = 4.0) -> None:
            return None

        @agent
        def annotated(reading: dict, me: "FunctionAgent | None" = None) -> None:
            return None

        @agent
        def by_class(reading: dict, me: FunctionAgent = None) -> None:  # pyright: ignore[reportArgumentType]
            return None

        @agent
        def dotted(reading: dict, me: "wactorz.FunctionAgent" = None) -> None:  # pyright: ignore[reportArgumentType]
            return None

        assert not _spec(detect).wants_actor
        assert _spec(annotated).wants_actor
        assert _spec(by_class).wants_actor
        assert _spec(dotted).wants_actor

    def test_anything_else_has_no_spec(self) -> None:
        assert spec_of(len) is None
        assert spec_of(object()) is None


class TestOnStart:
    async def test_it_subscribes_to_each_topic_and_publishes_a_manifest(
        self, tmp_path: Path, published: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @agent(subscribes=["a/one", "b/#"], publishes="out/x", description="d")
        def fn(reading: dict) -> dict:
            return reading

        actor = _build(fn, tmp_path)
        subscribed: list[str] = []
        monkeypatch.setattr(
            actor, "subscribe", lambda topic, cb, concurrency=1: subscribed.append(topic)
        )

        await actor.on_start()

        assert subscribed == ["a/one", "b/#"]
        manifest = next(p for t, p in published if t.endswith("/manifest"))
        assert manifest["publishes"] == ["out/x"]
        assert manifest["subscribes"] == ["a/one", "b/#"]
        assert manifest["description"] == "d"
        feed = next(p for t, p in published if t.endswith("/logs"))
        assert feed["message"] == "Listening on a/one, b/#"


class TestMessages:
    async def test_what_the_function_returns_is_published(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        @agent(subscribes="sensors/imu", publishes="anomalies/imu")
        def detect(reading: dict) -> dict | None:
            return reading if reading["ax"] > 1 else None

        actor = _build(detect, tmp_path)
        await actor._on_message({"ax": 0.5})
        await actor._on_message({"ax": 2.0})

        data = [(t, p) for t, p in published if not t.startswith("agents/")]
        assert data == [("anomalies/imu", {"ax": 2.0})]
        # Every message counts on the card; only what is published is on the feed.
        assert actor.metrics.messages_processed == 2
        assert actor.metrics.tasks_completed == 2
        feed = [p["message"] for t, p in published if t.endswith("/logs")]
        assert feed == ['→ anomalies/imu: {"ax": 2.0}']

    async def test_a_plain_function_runs_off_the_loop(self, tmp_path: Path) -> None:
        threads: list[str] = []

        @agent
        def slow(reading: dict) -> None:
            threads.append(threading.current_thread().name)

        actor = _build(slow, tmp_path)
        await actor._on_message({})

        assert threads[0] != threading.main_thread().name

    async def test_a_coroutine_function_runs_on_the_loop(self, tmp_path: Path) -> None:
        @agent
        async def quick(reading: dict) -> dict:
            await asyncio.sleep(0)
            return {"ok": True}

        actor = _build(quick, tmp_path)
        assert await actor.call({}) == {"ok": True}

    async def test_the_actor_is_passed_when_asked_for(self, tmp_path: Path) -> None:
        @agent
        def remember(reading: dict, me: FunctionAgent) -> dict:
            me.persist("last", reading)
            return {"threshold": me.options.get("threshold")}

        actor = _build(remember, tmp_path, options={"threshold": 3})
        assert await actor.call({"ax": 1}) == {"threshold": 3}
        assert actor.recall("last") == {"ax": 1}

    async def test_a_default_keeps_its_value_on_every_message(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        @agent(subscribes="sensors/imu", publishes="anomalies/imu")
        def detect(reading: dict, threshold: float = 4.0) -> dict | None:
            return reading if reading["score"] > threshold else None

        actor = _build(detect, tmp_path)
        await actor._on_message({"score": 5.0})

        assert ("anomalies/imu", {"score": 5.0}) in published

    async def test_a_coroutine_behind_a_plain_decorator_is_awaited(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        def plain(fn: Any) -> Any:
            def wrapper(reading: dict) -> Any:
                return fn(reading)

            return wrapper

        async def score(reading: dict) -> dict:
            await asyncio.sleep(0)
            return {"score": reading["ax"] * 2}

        detect = agent(subscribes="sensors/imu", publishes="scores/imu")(plain(score))
        actor = _build(detect, tmp_path)

        assert await actor.call({"ax": 1}) == {"score": 2}
        await actor._on_message({"ax": 3})
        assert ("scores/imu", {"score": 6}) in published


class TestTasks:
    async def test_a_task_is_answered_with_the_result_and_its_id(self, tmp_path: Path) -> None:
        @agent
        def classify(payload: dict) -> dict:
            return {"label": "walking", "input": payload}

        actor = _build(classify, tmp_path)
        registry = FakeRegistry()
        actor._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        msg = Message(
            type=MessageType.TASK,
            sender_id="main-id",
            payload={"_task_id": "t1", "ax": 1},
            reply_to="main-id",
        )
        await actor.handle_message(msg)

        target, reply = registry.delivered[0]
        assert target == "main-id"
        assert reply.type == MessageType.RESULT
        assert reply.payload == {"label": "walking", "input": {"ax": 1}, "_task_id": "t1"}
        assert actor.metrics.tasks_completed == 1

    async def test_a_non_dict_result_is_wrapped(self, tmp_path: Path) -> None:
        @agent
        def score(payload: dict) -> float:
            return 0.9

        actor = _build(score, tmp_path)
        registry = FakeRegistry()
        actor._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        await actor.handle_message(Message(type=MessageType.TASK, sender_id="s", payload={}))

        assert registry.delivered[0][1].payload == {"result": 0.9}

    async def test_a_failing_function_answers_with_the_error(self, tmp_path: Path) -> None:
        @agent
        def broken(payload: dict) -> None:
            raise ValueError("bad input")

        actor = _build(broken, tmp_path)
        registry = FakeRegistry()
        actor._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        await actor.handle_message(
            Message(type=MessageType.TASK, sender_id="s", payload={"_task_id": "t2"})
        )

        assert registry.delivered[0][1].payload == {"error": "bad input", "_task_id": "t2"}
        assert actor.metrics.tasks_failed == 1

    async def test_other_message_types_are_ignored(self, tmp_path: Path) -> None:
        @agent
        def fn(payload: dict) -> dict:
            return payload

        actor = _build(fn, tmp_path)
        registry = FakeRegistry()
        actor._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        await actor.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="s"))

        assert registry.delivered == []
