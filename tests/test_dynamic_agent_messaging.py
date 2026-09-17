"""How generated agent code gets something out: publish, log, alert, delegate.

`publish` does more than publish. It records the topic in the agent's manifest
the first time it is used, and records the field names of every dict payload on
the agent's topic contract, so the planner wires agents by what they really send
rather than by what they declared. Failing to reach the topic bus must never
cost the publish itself.

`send_to` tries the local registry first and a remote node second. Both routes
wrap a bare payload in a dict, both answer a timeout with an error dict rather
than raising, and both drop their pending future on the way out.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import messaging
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core import topic_bus
from wactorz.core.actor import Actor, Message, MessageType
from wactorz.core.registry import ActorRegistry
from wactorz.core.topic_bus import TopicBus, TopicContract


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload), retain))

    def on(self, topic: str) -> list[Any]:
        return [payload for t, payload, _ in self.published if t == topic]


class _Worker(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


class _Main:
    """Only what the remote route reads from the orchestrator."""

    def __init__(self, known_nodes: dict[str, dict[str, Any]]) -> None:
        self._known_nodes = known_nodes


class _Reply:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _ReplyClient:
    """A broker connection that answers the subscribed reply topic with `reply`."""

    def __init__(self, reply: bytes | None) -> None:
        self._reply = reply
        self.subscribed: list[str] = []

    async def __aenter__(self) -> "_ReplyClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> AsyncIterator[_Reply]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[_Reply]:
        if self._reply is None:
            await asyncio.Event().wait()
        yield _Reply(self._reply or b"")


@pytest.fixture(name="broker")
def broker_fixture() -> _Broker:
    return _Broker()


@pytest.fixture(name="api")
async def api_fixture(tmp_path: Path, broker: _Broker) -> AgentAPI:
    actor = DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path))
    actor._mqtt_client = broker
    await ActorRegistry().register(actor)
    return AgentAPI(actor)


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> TopicBus:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    return bus


@pytest.fixture(name="no_bus", autouse=True)
def no_bus_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without the process-wide bus; `bus` installs one."""
    monkeypatch.setattr(topic_bus, "_topic_bus", None)


async def _until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds, failing rather than spinning forever."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _finish(task: "asyncio.Task[Any]") -> Any:
    # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done, "the call did not finish"
    return task.result()


class TestPublish:
    async def test_the_payload_reaches_the_topic(self, api: AgentAPI, broker: _Broker) -> None:
        await api.publish("sensors/temp", {"temp": 21.5})

        assert broker.on("sensors/temp") == [{"temp": 21.5}]

    async def test_a_new_topic_is_announced_once(self, api: AgentAPI, broker: _Broker) -> None:
        await api.publish("sensors/temp", {"temp": 1})
        await api.publish("sensors/temp", {"temp": 2})

        (manifest,) = broker.on(f"agents/{api.actor_id}/manifest")
        assert manifest["publishes"] == ["sensors/temp"]
        assert all(retain for t, _, retain in broker.published if t.endswith("/manifest"))

    async def test_each_new_topic_updates_the_manifest(
        self, api: AgentAPI, broker: _Broker
    ) -> None:
        await api.publish("b/topic", 1)
        await api.publish("a/topic", 2)

        manifests = broker.on(f"agents/{api.actor_id}/manifest")
        assert [m["publishes"] for m in manifests] == [["b/topic"], ["a/topic", "b/topic"]]

    async def test_a_first_publish_creates_a_contract_from_the_payload(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        await api.publish("sensors/temp", {"temp": 21.5, "_internal": True})

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.publishes == ["sensors/temp"]
        assert contract.actor_id == api.actor_id
        assert contract.produces_schema == {"temp": "float"}

    async def test_a_payload_that_is_not_a_dict_has_no_schema(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        await api.publish("sensors/raw", [1, 2, 3])

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.publishes == ["sensors/raw"]
        assert contract.produces_schema == {}

    async def test_an_existing_contract_gains_the_topic_and_its_fields(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        bus.register_contract(
            TopicContract(name="probe", publishes=["declared"], produces_schema={"old": "str"})
        )

        await api.publish("sensors/temp", {"temp": 21.5})
        await api.publish("sensors/temp", {"temp": 22, "unit": "C"})

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.publishes == ["declared", "sensors/temp"]
        assert contract.produces_schema == {"old": "str", "temp": "int", "unit": "str"}
        assert contract.observed_samples["sensors/temp"]["example"] == {"temp": 22, "unit": "C"}

    async def test_a_topic_already_in_the_contract_is_not_repeated(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        bus.register_contract(TopicContract(name="probe", publishes=["sensors/temp"]))

        await api.publish("sensors/temp", {"temp": 1})

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.publishes == ["sensors/temp"]

    async def test_a_broken_bus_does_not_cost_the_publish(
        self, api: AgentAPI, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> TopicBus:
            raise RuntimeError("bus is gone")

        monkeypatch.setattr(topic_bus, "get_topic_bus", _broken)

        await api.publish("sensors/temp", {"temp": 1})

        assert broker.on("sensors/temp") == [{"temp": 1}]
        assert len(broker.on(f"agents/{api.actor_id}/manifest")) == 1


class TestConvenienceTopics:
    async def test_detections(self, api: AgentAPI, broker: _Broker) -> None:
        await api.publish_detection({"label": "cat"})

        assert broker.on(f"agents/{api.actor_id}/detections") == [{"label": "cat"}]

    async def test_results(self, api: AgentAPI, broker: _Broker) -> None:
        await api.publish_result({"answer": 42})

        assert broker.on(f"agents/{api.actor_id}/result") == [{"answer": 42}]

    async def test_a_log_line_keeps_its_text_for_the_dashboard(
        self, api: AgentAPI, broker: _Broker
    ) -> None:
        await api.log("température ✓")

        (entry,) = broker.on(f"agents/{api.actor_id}/logs")
        assert entry["type"] == "log"
        assert entry["message"] == "température ✓"
        assert entry["name"] == "probe"

    async def test_an_unknown_log_level_still_logs(
        self, api: AgentAPI, broker: _Broker, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger=messaging.__name__):
            await api.log("hello", level="shout")

        assert "[probe] hello" in caplog.text
        assert len(broker.on(f"agents/{api.actor_id}/logs")) == 1

    async def test_an_alert_names_the_agent(self, api: AgentAPI, broker: _Broker) -> None:
        await api.alert("too hot", severity="critical")

        (alert,) = broker.on(f"agents/{api.actor_id}/alert")
        assert alert["name"] == "probe"
        assert alert["actor_id"] == api.actor_id
        assert alert["message"] == "too hot"
        assert alert["severity"] == "critical"

    async def test_an_alert_is_a_warning_by_default(self, api: AgentAPI, broker: _Broker) -> None:
        await api.alert("hm")

        assert broker.on(f"agents/{api.actor_id}/alert")[0]["severity"] == "warning"

    async def test_a_user_notification_goes_to_the_chat_panel(
        self, api: AgentAPI, broker: _Broker
    ) -> None:
        await api.notify_user("done", task="backup")

        (chat,) = broker.on(f"agents/{api.actor_id}/chat")
        assert chat["content"] == "done"
        assert chat["from"] == "probe"
        assert chat["task"] == "backup"


class TestSendToLocal:
    async def test_without_a_registry_nothing_is_sent(self, tmp_path: Path) -> None:
        api = AgentAPI(DynamicAgent(name="alone", code="", persistence_dir=str(tmp_path)))

        assert await api.send_to("anyone", {}) is None

    async def test_the_reply_is_returned(self, api: AgentAPI, tmp_path: Path) -> None:
        target = _Worker(name="weather", persistence_dir=str(tmp_path))
        assert api._actor._registry is not None
        await api._actor._registry.register(target)

        call = asyncio.create_task(api.send_to("weather", {"city": "Athens"}))
        await _until(lambda: not target._mailbox.empty())
        msg = target._mailbox.get_nowait()
        api._actor._result_futures[msg.payload["_task_id"]].set_result({"temp": 30})

        assert await _finish(call) == {"temp": 30}
        assert msg.type == MessageType.TASK
        assert msg.payload["city"] == "Athens"
        assert msg.payload["_reply_to"] == api.actor_id
        assert api._actor._result_futures == {}

    async def test_a_bare_payload_is_wrapped(self, api: AgentAPI, tmp_path: Path) -> None:
        target = _Worker(name="echo", persistence_dir=str(tmp_path))
        assert api._actor._registry is not None
        await api._actor._registry.register(target)

        call = asyncio.create_task(api.send_to("echo", 7))
        await _until(lambda: not target._mailbox.empty())
        msg = target._mailbox.get_nowait()
        api._actor._result_futures[msg.payload["_task_id"]].set_result("ok")
        await _finish(call)

        assert msg.payload["message"] == 7
        assert msg.payload["text"] == "7"

    async def test_the_callers_payload_is_not_modified(self, api: AgentAPI, tmp_path: Path) -> None:
        target = _Worker(name="echo", persistence_dir=str(tmp_path))
        assert api._actor._registry is not None
        await api._actor._registry.register(target)
        payload = {"x": 1}

        result = await api.send_to("echo", payload, timeout=0.01)

        assert payload == {"x": 1}
        assert result == {"error": "Timeout waiting for 'echo'"}
        assert api._actor._result_futures == {}

    async def test_delegate_is_send_to(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, Any, float]] = []

        async def _send_to(name: str, payload: Any, timeout: float = 60.0) -> str:
            calls.append((name, payload, timeout))
            return "sent"

        monkeypatch.setattr(api, "send_to", _send_to)

        assert await api.delegate("planner", {"goal": "x"}, timeout=3) == "sent"
        assert calls == [("planner", {"goal": "x"}, 3)]

    async def test_many_results_come_back_in_order_with_failures_in_place(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _send_to(name: str, payload: Any, timeout: float = 60.0) -> Any:
            if name == "broken":
                raise RuntimeError("no")
            await asyncio.sleep(0.01 if name == "slow" else 0)
            return f"{name}:{payload}"

        monkeypatch.setattr(api, "send_to", _send_to)

        results = await api.send_to_many([("slow", 1), ("broken", 2), ("fast", 3)])

        assert results[0] == "slow:1"
        assert isinstance(results[1], RuntimeError)
        assert results[2] == "fast:3"


class TestSendToRemote:
    @staticmethod
    def _on_node(monkeypatch: pytest.MonkeyPatch, agents: list[str]) -> None:
        main = _Main({"rpi": {"agents": agents}})
        monkeypatch.setattr(messaging, "find_main_actor", lambda _registry: main)

    async def test_an_agent_no_node_has_is_reported(
        self, api: AgentAPI, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._on_node(monkeypatch, agents=["other"])

        result = await api.send_to("ghost", {})

        assert result == {"error": "Agent 'ghost' not found"}
        assert broker.published == []

    async def test_without_an_orchestrator_the_agent_is_not_found(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(messaging, "find_main_actor", lambda _registry: None)

        assert await api.send_to("ghost", {}) == {"error": "Agent 'ghost' not found"}

    async def test_the_remote_reply_is_returned(
        self, api: AgentAPI, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._on_node(monkeypatch, agents=["camera"])
        client = _ReplyClient(b'{"frames": 3}')
        monkeypatch.setattr(messaging, "mqtt_client", lambda _host, _port, **_kw: client)

        result = await api.send_to("camera", "snap", timeout=5)

        assert result == {"frames": 3}
        (task,) = broker.on("agents/by-name/camera/task")
        assert task["message"] == "snap"
        assert task["_remote_task"] is True
        assert client.subscribed == [task["_reply_topic"]]
        assert task["_reply_topic"].startswith("agents/by-name/probe/reply/")
        assert api._actor._result_futures == {}

    async def test_a_silent_node_times_out_with_an_error(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._on_node(monkeypatch, agents=["camera"])
        monkeypatch.setattr(
            messaging, "mqtt_client", lambda _host, _port, **_kw: _ReplyClient(None)
        )

        result = await api.send_to("camera", {}, timeout=0.05)

        assert result == {"error": "Timeout waiting for remote 'camera'"}
        assert api._actor._result_futures == {}


class TestAwaitRemoteReply:
    class _Actor:
        _mqtt_broker = "broker"
        _mqtt_port = 1883

    async def test_the_reply_resolves_the_future(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            messaging, "mqtt_client", lambda _h, _p, **_kw: _ReplyClient(b'{"ok": true}')
        )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        await messaging.await_remote_reply(future, "reply/1", self._Actor(), "cam", 1)

        assert future.result() == {"ok": True}

    async def test_an_unreadable_reply_leaves_the_future_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            messaging, "mqtt_client", lambda _h, _p, **_kw: _ReplyClient(b"not json")
        )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        await messaging.await_remote_reply(future, "reply/1", self._Actor(), "cam", 1)

        assert not future.done()

    async def test_a_future_already_settled_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            messaging, "mqtt_client", lambda _h, _p, **_kw: _ReplyClient(b'{"late": 1}')
        )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.set_result("first")

        await messaging.await_remote_reply(future, "reply/1", self._Actor(), "cam", 1)

        assert future.result() == "first"

    async def test_a_broker_that_refuses_fails_the_future(self) -> None:
        # The suite-wide fixture refuses every connection, which is this case.
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        await messaging.await_remote_reply(future, "reply/1", self._Actor(), "cam", 1)

        with pytest.raises(ConnectionRefusedError):
            future.result()
