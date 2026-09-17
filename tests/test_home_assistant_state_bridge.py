"""The bridge from Home Assistant state changes to MQTT topics.

Each state change is published to a topic per entity, or all to one topic, as
configured, and a domain filter drops changes the operator did not ask for before
anything is published. The listener reconnects after an error and keeps the
error for the status reply, and a bridge with no Home Assistant configured says
so instead of retrying a connection it cannot make.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wactorz import config
from wactorz.agents import home_assistant_state_bridge_agent as bridge_mod
from wactorz.agents.home_assistant_state_bridge_agent import (
    HomeAssistantStateBridgeAgent,
    _parse_domains,
)
from wactorz.core.actor import ActorState, Message, MessageType


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload)))


def _bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> HomeAssistantStateBridgeAgent:
    for name in (
        "HOME_ASSISTANT_URL",
        "HOME_ASSISTANT_TOKEN",
        "HA_STATE_BRIDGE_OUTPUT_TOPIC",
        "HA_STATE_BRIDGE_DOMAINS",
        "HA_STATE_BRIDGE_PER_ENTITY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # Pinned rather than read: CONFIG is built from the developer's `.env`.
    monkeypatch.setattr(
        bridge_mod,
        "CONFIG",
        replace(
            config.CONFIG,
            ha_url="",
            ha_token="",
            ha_state_bridge_output_topic="",
            ha_state_bridge_domains="",
            ha_state_bridge_per_entity=False,
        ),
    )
    bridge = HomeAssistantStateBridgeAgent(persistence_dir=str(tmp_path))
    bridge._mqtt_client = _Broker()
    return bridge


def _published(bridge: HomeAssistantStateBridgeAgent) -> list[tuple[str, Any]]:
    broker = bridge._mqtt_client
    assert isinstance(broker, _Broker)
    return broker.published


def _event(entity_id: str) -> dict[str, Any]:
    return {
        "event": {"data": {"entity_id": entity_id, "new_state": {"state": "on"}, "old_state": None}}
    }


CONFIGURED = {
    "HOME_ASSISTANT_URL": "http://ha:8123",
    "HOME_ASSISTANT_TOKEN": "t",
    "HA_STATE_BRIDGE_PER_ENTITY": "1",
}


class TestPublishing:
    async def test_a_change_goes_to_its_entity_topic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(tmp_path, monkeypatch, **CONFIGURED)

        await bridge._handle_state_change(_event("light.hall"))

        ((topic, payload),) = _published(bridge)
        assert topic == "homeassistant/state_changes/light/light.hall"
        assert payload["domain"] == "light" and payload["new_state"] == {"state": "on"}
        assert bridge._events_seen == 1

    async def test_one_topic_and_a_domain_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(
            tmp_path,
            monkeypatch,
            HA_STATE_BRIDGE_PER_ENTITY="no",
            HA_STATE_BRIDGE_DOMAINS="Light, switch ,",
            HA_STATE_BRIDGE_OUTPUT_TOPIC="ha/all",
        )

        await bridge._handle_state_change(_event("sensor.t"))
        await bridge._handle_state_change(_event("switch.fan"))
        await bridge._handle_state_change({})

        assert [topic for topic, _ in _published(bridge)] == ["ha/all"]
        assert (
            bridge._current_task_description()
            == "watching state_changed domains=light, switch (1 seen)"
        )

    def test_domains_are_parsed_loosely(self) -> None:
        assert _parse_domains(" Light,,SWITCH ") == {"light", "switch"}


class TestStatus:
    async def test_status_is_answered_in_words_and_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(tmp_path, monkeypatch, **CONFIGURED)
        bridge._events_seen = 1
        bridge._last_error = "socket closed"
        sent: list[Any] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append(payload)
            return True

        monkeypatch.setattr(bridge, "send", _send)

        await bridge.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"command": " STATUS "})
        )
        await bridge.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload="restart")
        )
        await bridge.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="main"))

        assert sent[0]["configured"] is True
        assert (
            "I have seen 1 state change so far. The last thing to go wrong: socket closed."
            in sent[0]["result"]
        )
        assert sent[1]["supported_commands"] == ["status"]
        assert bridge.metrics.tasks_failed == 1
        assert bridge._current_task_description().startswith("waiting for HA state changes (error:")

    def test_an_unconfigured_bridge_says_it_watches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(tmp_path, monkeypatch)
        bridge.ha_url = bridge.ha_token = ""

        assert "no instance is configured" in bridge._spoken_status(bridge._build_status_payload())

    async def test_counters_survive_a_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = _bridge(tmp_path, monkeypatch)
        first.ha_url = ""
        first._events_seen, first._last_error = 7, "x"
        await first.on_stop()

        second = _bridge(tmp_path, monkeypatch)
        second.ha_url = ""
        await second._load_persistent_state()
        await second.on_start()
        await asyncio.wait(second._tasks, timeout=5)

        assert second._events_seen == 7
        assert "not configured" in second._last_error


class _Ha:
    """Stands in for HAWebSocketClient: events, then a failure or a stop."""

    def __init__(
        self, bridge: HomeAssistantStateBridgeAgent, events: list[Any], fail: Exception | None
    ) -> None:
        self.bridge = bridge
        self.events = events
        self.fail = fail
        self.connections = 0

    def __call__(self, url: str, token: str) -> "_Ha":
        self.connections += 1
        return self

    async def __aenter__(self) -> "_Ha":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe_events(self, event_type: str) -> int:
        if self.fail and self.connections == 1:
            raise self.fail
        return 1

    async def receive_event(self, subscription_id: int) -> Any:
        if self.events:
            return self.events.pop(0)
        self.bridge.state = ActorState.STOPPED
        return _event("light.last")


class TestListener:
    async def test_events_are_published_until_the_bridge_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(tmp_path, monkeypatch, **CONFIGURED)
        bridge.state = ActorState.RUNNING
        monkeypatch.setattr(bridge_mod, "HAWebSocketClient", _Ha(bridge, [_event("light.a")], None))

        await bridge._state_change_listener()

        assert [p["entity_id"] for _, p in _published(bridge)] == ["light.a", "light.last"]

    async def test_an_error_is_kept_and_the_listener_reconnects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _bridge(tmp_path, monkeypatch, **CONFIGURED)
        bridge.state = ActorState.RUNNING
        ha = _Ha(bridge, [], ConnectionError("refused"))
        monkeypatch.setattr(bridge_mod, "HAWebSocketClient", ha)
        errors: list[str] = []
        real_sleep = asyncio.sleep

        async def _sleep(_delay: float) -> None:
            errors.append(bridge._last_error)
            await real_sleep(0)

        monkeypatch.setattr(bridge_mod.asyncio, "sleep", _sleep)

        await bridge._state_change_listener()

        assert errors == ["refused"]
        assert ha.connections == 2
        assert bridge._last_error == ""
