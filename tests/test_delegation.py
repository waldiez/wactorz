"""Handing a task to another agent and waiting for its answer.

Two routes, tried in that order: an agent running in this process gets the task
through its mailbox, and one running on another machine gets it over MQTT. An
agent that is neither is not a failure to report — the caller is told nothing
came back and decides what that means.

The reply topic is subscribed before the caller is allowed to publish, and that
ordering is the point. Publishing first means an agent that answers quickly
answers into a topic nobody is listening on: the broker drops the reply and the
caller waits out its whole timeout for work that in fact succeeded. It reads as
a flaky node rather than as a bug, which is how it survived.

Every waiting future is dropped on the way out. They are keyed per task, so
keeping them grows the table for the life of the process — and the process is
meant to run for months.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.agents.delegation import DelegationManager
from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.nodes import NodeManager
from wactorz.core.actor import MessageType

_SUBSCRIBE_FAILED = "broker refused the subscription"


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


class _Registry:
    def __init__(self, running: tuple[str, ...] = ()) -> None:
        self._by = {n: _Agent(n) for n in running}

    def find_by_name(self, name: str) -> _Agent | None:
        return self._by.get(name)


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.topic = "main/reply/x"
        self.payload = payload


class _Replies:
    """The `client.messages` stream: whatever the node sent back."""

    def __init__(self, replies: list[_Message]) -> None:
        self._replies = replies

    def __aiter__(self) -> "_Replies":
        return self

    async def __anext__(self) -> _Message:
        if not self._replies:
            # Nothing more is coming, but the listener must not fall out of its
            # loop and tear the subscription down while the caller still waits.
            await asyncio.sleep(3600)
        return self._replies.pop(0)


class _Client:
    """An MQTT client that hands over the replies it was given.

    Subscribing takes a moment, which is how a test can tell whether the caller
    waited for it. With no delay every ordering looks correct, because the
    listener gets to run before the first assertion either way.
    """

    def __init__(self, replies: list[_Message], subscribe_delay: float = 0.0) -> None:
        self.subscribed: list[str] = []
        self.messages = _Replies(replies)
        self._delay = subscribe_delay

    async def subscribe(self, topic: str) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.subscribed.append(topic)


class _Broker:
    """A stand-in for `mqtt_client`, which is an async context manager."""

    def __init__(
        self,
        replies: list[_Message] | None = None,
        fails: bool = False,
        subscribe_delay: float = 0.0,
    ) -> None:
        self.subscribed: list[str] = []
        self._replies = replies or []
        self._fails = fails
        self._delay = subscribe_delay
        self.client: _Client | None = None

    def __call__(self, _host: str, _port: int) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        if self._fails:
            raise RuntimeError(_SUBSCRIBE_FAILED)
        self.client = _Client(list(self._replies), self._delay)
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        if self.client is not None:
            self.subscribed = self.client.subscribed
        return False


class _Main:
    """A MainActor with the surface the delegation paths touch."""

    def __init__(
        self,
        *,
        running: tuple[str, ...] = (),
        known_nodes: dict[str, dict[str, Any]] | None = None,
        registry: bool = True,
        result: dict[str, Any] | None = None,
        answers: bool = True,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.delegation = DelegationManager(main)
        main._known_nodes = dict(known_nodes or {})
        main._mqtt_broker = "localhost"
        main._mqtt_port = 1883
        main._result_futures = {}
        setattr(main, "_registry", _Registry(running) if registry else None)

        self.sent: list[tuple[str, Any, dict[str, Any]]] = []
        self.published: list[tuple[str, Any]] = []

        async def _send(actor_id: str, kind: Any, payload: dict[str, Any]) -> None:
            self.sent.append((actor_id, kind, payload))
            if not answers:
                return
            future = main._result_futures.get(payload["_task_id"])
            if future is not None and not future.done():
                future.set_result(result if result is not None else {"ok": True})

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        setattr(main, "send", _send)
        setattr(main, "_mqtt_publish", _publish)
        self.actor = main

    async def delegate(self, name: str, task: str = "do it", timeout: float = 5.0) -> Any:
        return await self.actor.delegation.delegate_task(name, task, timeout=timeout)

    async def install(self, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        return await self.actor.delegation.delegate_to_installer(payload, timeout=timeout)


class TestChoosingWhereTheTaskGoes:
    """Local first, then a node that claims the agent, then nothing."""

    async def test_an_agent_in_this_process_gets_it_directly(self) -> None:
        main = _Main(running=("weather",))

        result = await main.delegate("weather")

        assert result == {"ok": True}
        assert main.sent[0][0] == "weather-id"
        assert not main.published

    async def test_it_is_sent_as_a_task(self) -> None:
        main = _Main(running=("weather",))

        await main.delegate("weather")

        assert main.sent[0][1] == MessageType.TASK

    async def test_the_target_is_told_where_to_answer(self) -> None:
        main = _Main(running=("weather",))

        await main.delegate("weather")

        assert main.sent[0][2]["reply_to"] == "main-id"

    async def test_an_agent_on_a_node_is_reached_over_mqtt(self) -> None:
        main = _Main(known_nodes={"rpi": {"agents": ["sensor"]}})

        await main.delegate("sensor", timeout=0.05)

        topic, payload = main.published[0]
        assert topic == "agents/by-name/sensor/task"
        assert payload["_remote_task"] is True

    async def test_a_local_agent_is_preferred_over_the_same_name_on_a_node(self) -> None:
        # The in-process mailbox is immediate and needs no broker.
        main = _Main(running=("sensor",), known_nodes={"rpi": {"agents": ["sensor"]}})

        await main.delegate("sensor")

        assert main.sent
        assert not main.published

    async def test_an_agent_nobody_knows_answers_nothing(self) -> None:
        main = _Main()

        assert await main.delegate("ghost") is None

    async def test_no_registry_at_all_answers_nothing(self) -> None:
        main = _Main(registry=False)

        assert await main.delegate("weather") is None


class TestWaitingForTheAnswer:
    """A task that never answers ends, and leaves nothing behind."""

    async def test_a_local_agent_that_never_answers_times_out(self) -> None:
        main = _Main(running=("weather",), answers=False)

        assert await main.delegate("weather", timeout=0.05) is None

    async def test_the_future_is_dropped_after_a_timeout(self) -> None:
        main = _Main(running=("weather",), answers=False)

        await main.delegate("weather", timeout=0.05)

        assert not main.actor._result_futures

    async def test_the_future_is_dropped_after_an_answer(self) -> None:
        main = _Main(running=("weather",))

        await main.delegate("weather")

        assert not main.actor._result_futures

    async def test_a_remote_agent_that_never_answers_times_out(self) -> None:
        main = _Main(known_nodes={"rpi": {"agents": ["sensor"]}})

        assert await main.delegate("sensor", timeout=0.05) is None


class TestAskingTheInstaller:
    """Deploys go through one agent, and it may not be there."""

    async def test_the_payload_is_forwarded_with_a_task_id(self) -> None:
        main = _Main(running=("installer",))

        await main.install({"action": "node_deploy", "host": "10.0.0.5"})

        payload = main.sent[0][2]
        assert payload["action"] == "node_deploy"
        assert payload["_task_id"]
        assert payload["task"] == payload["_task_id"]

    async def test_the_callers_payload_is_left_alone(self) -> None:
        # The caller may reuse it, and finding a task id in it later would make
        # a second call answer to the first one's reply.
        main = _Main(running=("installer",))
        payload = {"action": "node_deploy"}

        await main.install(payload)

        assert payload == {"action": "node_deploy"}

    async def test_a_missing_installer_is_reported_rather_than_awaited(self) -> None:
        main = _Main()

        assert "error" in await main.install({"action": "node_deploy"})

    async def test_no_registry_is_reported_too(self) -> None:
        main = _Main(registry=False)

        assert "error" in await main.install({"action": "node_deploy"})

    async def test_a_slow_deploy_says_it_timed_out(self) -> None:
        # Deploys are SSH and pip, so the caller needs the difference between
        # "still going" and "failed".
        main = _Main(running=("installer",), answers=False)

        result = await main.install({"action": "node_deploy"}, timeout=0.05)

        assert "timed out" in result["error"]

    async def test_the_future_is_dropped_when_the_deploy_times_out(self) -> None:
        main = _Main(running=("installer",), answers=False)

        await main.install({"action": "node_deploy"}, timeout=0.05)

        assert not main.actor._result_futures


class TestTheReplyTopic:
    """Subscribed before the caller publishes, and cleaned up after."""

    async def test_it_subscribes_before_handing_the_topic_over(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Publishing first loses the answer of any agent quick enough to reply
        # before the subscription exists, and the caller then waits out its
        # whole timeout on work that succeeded.
        main = _Main()
        broker = _Broker(subscribe_delay=0.05)
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", broker)

        async with main.actor.delegation._reply_topic() as (topic, _future):
            # Checked the instant the topic is handed over, with no chance for
            # the listener to catch up: the subscription must already exist.
            assert broker.client is not None
            assert broker.client.subscribed == [topic]

    async def test_the_future_is_registered_under_the_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", _Broker())

        async with main.actor.delegation._reply_topic() as (topic, future):
            assert main.actor._result_futures[topic] is future

    async def test_the_topic_is_forgotten_on_the_way_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", _Broker())

        async with main.actor.delegation._reply_topic() as (_topic, _future):
            pass

        assert not main.actor._result_futures

    async def test_a_reply_lands_in_the_future(self, monkeypatch: pytest.MonkeyPatch) -> None:
        main = _Main()
        broker = _Broker(replies=[_Message(json.dumps({"answer": 42}).encode())])
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", broker)

        async with main.actor.delegation._reply_topic() as (_topic, future):
            assert await asyncio.wait_for(future, timeout=1) == {"answer": 42}

    async def test_the_caller_still_gets_a_topic_when_the_broker_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Losing the answer is worse than losing the work: the task is sent
        # anyway, and the reason is logged so the timeout that follows is
        # explained rather than mysterious.
        main = _Main()
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", _Broker(fails=True))

        async with main.actor.delegation._reply_topic() as (topic, future):
            assert topic.startswith("main/reply/main-id/")
            # The failure is carried on the future rather than raised here, so
            # the caller still has somewhere to publish. Retrieved because an
            # exception nobody reads is reported by asyncio when it is
            # collected, in whichever test happens to be running by then.
            with pytest.raises(RuntimeError, match=_SUBSCRIBE_FAILED):
                await future

    async def test_every_topic_is_its_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        main = _Main()
        monkeypatch.setattr("wactorz.agents.delegation.mqtt_client", _Broker())
        seen = []

        for _ in range(3):
            async with main.actor.delegation._reply_topic() as (topic, _f):
                seen.append(topic)

        assert len(set(seen)) == 3
