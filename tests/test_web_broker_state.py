"""The dashboard's view of the broker: topics folded into state, and deletes sent out.

Every retained or live frame is folded into one in-memory map the browsers are
shown, so each topic updates exactly the fields it is about and never
resurrects an agent that was deleted. Deleting goes the other way: through main
when it is here, through the local registry when it is not, and over the broker
as a last resort — to the node that hosts a remote agent, or to the agent's own
command topic — and the retained frames are cleared either way.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.web import events, lifecycle, mqtt, runtime, ws


class _Broker:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, Any]] = []
        self.fail = fail

    async def publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("broker gone")
        self.published.append((topic, payload))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        runtime,
        "state",
        {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
    )
    monkeypatch.setattr(runtime, "deleted_agent_ids", [])
    monkeypatch.setattr(runtime, "registry", None)
    monkeypatch.setattr(runtime, "mqtt_client_ref", None)
    monkeypatch.setattr(runtime, "db", None)
    monkeypatch.setattr(runtime, "hard_resetting", False)
    sent: list[dict[str, Any]] = []

    async def _broadcast(msg: dict[str, Any]) -> None:
        sent.append(msg)

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return sent


def _parse(topic: str, payload: Any) -> Any:
    return events.parse_topic(topic, payload if isinstance(payload, str) else json.dumps(payload))


class TestTopics:
    def test_system_health_and_alerts_are_kept_bounded(self) -> None:
        _parse("system/health", {"running": 3})
        for i in range(55):
            _parse("system/alerts", {"n": i})

        assert runtime.state["system_health"] == {"running": 3}
        assert len(runtime.state["alerts"]) == 50
        assert runtime.state["alerts"][0] == {"n": 54}

    def test_status_carries_name_state_and_flags(self) -> None:
        _parse(
            "agents/a1/status",
            {"name": "weather", "state": "running", "protected": True, "essential": False},
        )

        agent = runtime.state["agents"]["a1"]
        assert (agent["name"], agent["state"], agent["protected"], agent["essential"]) == (
            "weather",
            "running",
            True,
            False,
        )
        assert runtime.state["log_feed"][0]["type"] == "status"

    def test_metrics_record_cost_and_tokens(self) -> None:
        _parse("agents/a1/status", {"name": "weather"})
        _parse(
            "agents/a1/metrics",
            {"messages_processed": 4, "cost_usd": 0.25, "input_tokens": 10, "output_tokens": 2},
        )

        agent = runtime.state["agents"]["a1"]
        assert (agent["messages_processed"], agent["cost_usd"], agent["input_tokens"]) == (
            4,
            0.25,
            10,
        )

    def test_feed_rows_are_named_after_the_agent(self) -> None:
        _parse("agents/a1/status", {"name": "weather"})
        _parse("agents/a1/spawned", {"child_name": "helper"})
        _parse("agents/a1/completed", {"ok": True})
        _parse("agents/a1/logs", "plain text")

        rows = runtime.state["log_feed"]
        assert [row["type"] for row in rows[:3]] == ["log", "completed", "spawned"]
        assert {row["name"] for row in rows[:3]} == {"weather"}

    def test_an_alert_is_attributed_and_bounded(self) -> None:
        for _ in range(51):
            _parse("agents/a1/alert", "not a dict")
        _parse("agents/a2/alert", {"severity": "critical"})

        assert runtime.state["alerts"][0] == {
            "severity": "critical",
            "agent_id": "a2",
            "name": "a2",
        }
        assert len(runtime.state["alerts"]) == 50
        assert runtime.state["log_feed"][0]["message"] == "a2 unresponsive (critical)"

    def test_a_chat_frame_with_a_bad_timestamp_is_still_shown(self) -> None:
        event = _parse(
            "agents/a1/chat", {"content": "done", "timestamp": "soon", "source": "voice"}
        )

        assert event["_push_chat"]["content"] == "done"
        assert event["_push_chat"]["source"] == "voice"

    def test_a_node_heartbeat_without_a_body_is_ignored(self) -> None:
        assert _parse("nodes/rpi/heartbeat", "not json") is None
        assert runtime.state["nodes"] == {}
        assert _parse("unknown/topic", "x") is None

    def test_a_stale_status_does_not_bring_a_deleted_agent_back(self) -> None:
        runtime.mark_deleted("a1")

        event = _parse(
            "agents/a1/status", {"name": "weather", "uptime": "not a number", "state": "stopped"}
        )

        assert runtime.state["agents"] == {}
        assert event["subtype"] == "status"

    @pytest.mark.parametrize(("uptime", "back"), [(9.9, True), (10.0, False)])
    def test_only_a_status_from_a_fresh_start_brings_a_deleted_agent_back(
        self, uptime: float, back: bool
    ) -> None:
        runtime.mark_deleted("a1")

        _parse("agents/a1/status", {"name": "weather", "uptime": uptime, "state": "running"})

        assert ("a1" in runtime.state["agents"]) is back

    def test_another_system_topic_is_relayed_and_changes_nothing(self) -> None:
        event = _parse("system/spawn", {"name": "x"})

        assert event == {"type": "system", "subtype": "spawn", "data": {"name": "x"}}
        assert runtime.state["system_health"] == {}
        assert runtime.state["alerts"] == []

    def test_a_status_that_is_not_an_object_is_kept_as_it_came(self) -> None:
        _parse("agents/a1/status", "starting")

        agent = runtime.state["agents"]["a1"]
        assert (agent["status"], agent["name"]) == ("starting", "a1")
        assert runtime.state["log_feed"][0]["status"] == "starting"

    def test_a_heartbeat_during_a_hard_reset_adds_no_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "hard_resetting", True)

        event = _parse("agents/a1/heartbeat", {"name": "weather"})

        assert runtime.state["agents"] == {}
        assert event["metric"] == "heartbeat"

    def test_a_chat_frame_that_is_plain_text_is_shown_from_the_agent(self) -> None:
        _parse("agents/a1/status", {"name": "weather"})

        event = _parse("agents/a1/chat", "  It will rain.  ")

        assert event["_push_chat"]["content"] == "It will rain."
        assert event["_push_chat"]["from"] == "weather"
        assert runtime.state["log_feed"][0]["type"] == "chat"

    def test_a_chat_frame_with_nothing_to_say_is_relayed_but_not_shown(self) -> None:
        event = _parse("agents/a1/chat", "42")

        assert event == {"type": "agent", "agent_id": "a1", "metric": "chat", "data": 42}
        assert runtime.state["log_feed"] == []

    def test_a_question_keeps_its_id_options_and_channel_on_the_way_to_the_chat(self) -> None:
        event = _parse(
            "agents/a1/chat",
            {"content": "Install?", "id": "q1", "options": ["yes", "no"], "channel": "telegram"},
        )

        pushed = event["_push_chat"]
        assert (pushed["id"], pushed["options"], pushed["channel"]) == (
            "q1",
            ["yes", "no"],
            "telegram",
        )


class _Actor:
    def __init__(
        self, actor_id: str, name: str, protected: bool = False, accept: bool = True
    ) -> None:
        self.actor_id = actor_id
        self.name = name
        self.protected = protected
        self.accept = accept
        self.commands: list[str] = []

    async def apply_command(self, command: str) -> bool:
        self.commands.append(command)
        return self.accept


class _Registry:
    def __init__(self, *actors: _Actor) -> None:
        self._actors = list(actors)

    def get(self, actor_id: str) -> _Actor | None:
        return next((a for a in self._actors if a.actor_id == actor_id), None)

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors if a.name == name), None)


class _Main:
    def __init__(self, fail: bool = False, kept: tuple[dict[str, Any], ...] = ()) -> None:
        self.deleted: list[str] = []
        #: Which agent of its name each delete named: its node, "" for main.
        self.nodes: list[str | None] = []
        self.fail = fail
        self.kept = kept

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry({entry["actor_id"]: entry for entry in self.kept})

    async def delete_spawned_agent(self, name: str, node: str | None = None) -> None:
        if self.fail:
            raise RuntimeError("spawn registry locked")
        self.deleted.append(name)
        self.nodes.append(node)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestDelete:
    async def test_a_protected_actor_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "main", protected=True)))
        runtime.state["agents"]["a1"] = {"name": "main"}

        assert await lifecycle.delete_agent("a1") == "refused-protected"
        assert "a1" in runtime.state["agents"]

    async def test_main_does_the_delete_when_it_is_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        monkeypatch.setattr(runtime, "registry", _Registry())
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _r: main)
        runtime.state["agents"]["a1"] = {"name": "weather"}

        routed = await lifecycle.delete_agent("a1")

        assert routed == "via main.delete_spawned_agent('weather')"
        assert main.deleted == ["weather"]
        assert runtime.is_deleted("a1") and runtime.state["agents"] == {}

    async def test_the_agent_deleted_is_the_one_its_id_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two agents called weather: the dashboard deletes by id, and main is
        # told which one by where its registry keeps that id.
        main = _Main(
            kept=(
                {"name": "weather", "node": "", "actor_id": "here"},
                {"name": "weather", "node": "pi", "actor_id": "a1"},
            )
        )
        monkeypatch.setattr(runtime, "registry", _Registry())
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _r: main)
        runtime.state["agents"]["a1"] = {"name": "weather"}

        await lifecycle.delete_agent("a1")

        assert (main.deleted, main.nodes) == (["weather"], ["pi"])

    async def test_a_failing_main_falls_back_to_the_local_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor = _Actor("a1", "weather")
        monkeypatch.setattr(runtime, "registry", _Registry(actor))
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _r: _Main(fail=True))

        assert await lifecycle.delete_agent("a1") == "via local registry"
        assert actor.commands == ["delete"]

    async def test_a_remote_agent_is_stopped_through_its_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker()
        monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
        runtime.state["agents"]["r1"] = {"name": "cam", "node": "rpi"}

        routed = await lifecycle.delete_agent("r1")
        await _settle()

        assert routed == "via nodes/rpi/stop"
        assert broker.published[0] == ("nodes/rpi/stop", json.dumps({"name": "cam"}))
        assert ("agents/r1/status", b"") in broker.published

    async def test_an_unknown_local_agent_is_told_on_its_command_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker()
        monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("other", "x", accept=False)))
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _r: None)

        routed = await lifecycle.delete_agent("a9")

        assert routed == "via agents/a9/commands"
        assert json.loads(broker.published[0][1])["command"] == "stop"

    @pytest.mark.parametrize("record", [{"name": "cam", "node": "rpi"}, {"name": "cam"}])
    async def test_a_broker_that_fails_leaves_the_delete_unrouted(
        self, monkeypatch: pytest.MonkeyPatch, record: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(runtime, "mqtt_client_ref", _Broker(fail=True))
        runtime.state["agents"]["r1"] = record

        routed = await lifecycle.delete_agent("r1")
        await _settle()

        assert routed == "unknown"


class TestDispatch:
    async def test_a_remote_command_needs_a_broker_that_accepts_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert await lifecycle.dispatch_command("r1", "stop", "rest") == ""

        monkeypatch.setattr(runtime, "mqtt_client_ref", _Broker(fail=True))
        assert await lifecycle.dispatch_command("r1", "stop", "rest") == ""

        broker = _Broker()
        monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
        assert await lifecycle.dispatch_command("r1", "stop", "rest") == "broker"
        assert broker.published[0][0] == "agents/r1/commands"


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    """Refuses the first connection, then delivers messages and ends the run."""

    def __init__(self) -> None:
        self.connections = 0
        self.subscribed: list[str] = []
        self.published: list[str] = []

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Client":
        self.connections += 1
        if self.connections == 1:
            raise ConnectionRefusedError("not yet")
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        self.published.append(topic)

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        yield _Message("agents/a1/status", json.dumps({"name": "weather"}).encode())
        # How the server's task ends: cancelled, which the listener must not swallow.
        raise asyncio.CancelledError


class TestListener:
    async def test_it_reconnects_subscribes_and_folds_messages_in(
        self, monkeypatch: pytest.MonkeyPatch, _isolated: list[dict[str, Any]]
    ) -> None:
        client = _Client()
        monkeypatch.setattr(mqtt, "mqtt_client", client)
        monkeypatch.setattr(runtime, "registry", _Registry())
        monkeypatch.setattr(runtime, "mqtt_connected", None)
        real_sleep = asyncio.sleep

        async def _instant(_delay: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr(mqtt.asyncio, "sleep", _instant)

        with pytest.raises(asyncio.CancelledError):
            await mqtt.mqtt_listener()

        assert client.connections == 2
        # The chat transport is not announced as an agent: one that is reaches
        # the dashboard's agent list and is offered as someone to talk to.
        assert client.published == []
        assert client.subscribed == list(runtime.MQTT_TOPICS)
        assert runtime.state["agents"]["a1"]["name"] == "weather"
        assert runtime.mqtt_client_ref is None
        assert [m["connected"] for m in _isolated if m.get("type") == "mqtt_status"] == [
            False,
            True,
        ]
