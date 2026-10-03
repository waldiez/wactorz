"""The reactive actuator: an MQTT trigger in, Home Assistant service calls out.

Every detection goes through the same four gates, in order: the payload filter,
the cooldown, the live entity conditions, then one service call per action with
`$payload` references filled in from the trigger. An action whose reference does
not resolve is skipped rather than sent with a hole in it, because Home
Assistant would reject it — or worse, accept a null.

Conditions fail open when there is no Home Assistant connection, and fail closed
when there is one and the entity is missing or the lookup raises. That asymmetry
is deliberate: no connection means no way to act either, while a connection that
cannot find the entity is evidence the condition does not hold.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wactorz.agents import home_assistant_actuator_agent as module
from wactorz.agents.home_assistant_actuator_agent import (
    ActuatorAction,
    ActuatorCondition,
    ActuatorConfig,
    HomeAssistantActuatorAgent,
    resolve_payload_refs,
)
from wactorz.core.actor import ActorState, Message, MessageType


class _HA:
    """A Home Assistant connection that records service calls."""

    def __init__(self, states: dict[str, dict[str, Any]] | None = None) -> None:
        self.states = states or {}
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []
        self.fail_calls = False
        self.fail_lookups = False

    async def get_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        if self.fail_lookups:
            raise RuntimeError("websocket closed")
        return self.states.get(entity_id)

    async def call_service(self, domain: str, service: str, entity_id: str, **data: Any) -> None:
        if self.fail_calls:
            raise RuntimeError("service not found")
        self.calls.append((domain, service, entity_id, data))


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload)))


def _config(**overrides: Any) -> ActuatorConfig:
    values: dict[str, Any] = {
        "automation_id": "fan",
        "description": "Turns the fan on.",
        "mqtt_topics": ["sensors/nursery"],
        "actions": [ActuatorAction("fan", "turn_on", "fan.nursery")],
        "cooldown_seconds": 0,
    }
    values.update(overrides)
    return ActuatorConfig(**values)


def _agent(tmp_path: Path, **overrides: Any) -> HomeAssistantActuatorAgent:
    agent = HomeAssistantActuatorAgent(config=_config(**overrides), persistence_dir=str(tmp_path))
    agent._mqtt_client = _Broker()
    return agent


def _published(agent: HomeAssistantActuatorAgent) -> list[tuple[str, Any]]:
    broker = agent._mqtt_client
    assert isinstance(broker, _Broker)
    return broker.published


class TestPayloadReferences:
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "room": "nursery",
        "reading": {"temp": 27.5},
        "people": ["ada", "bob"],
    }

    def test_a_plain_value_is_unchanged(self) -> None:
        assert resolve_payload_refs("red", self.PAYLOAD) == "red"
        assert resolve_payload_refs(3, self.PAYLOAD) == 3

    def test_the_bare_reference_is_the_whole_payload(self) -> None:
        assert resolve_payload_refs("$payload", self.PAYLOAD) is self.PAYLOAD

    def test_a_dotted_reference_reaches_into_dicts(self) -> None:
        assert resolve_payload_refs("$payload.reading.temp", self.PAYLOAD) == 27.5

    def test_a_numeric_part_indexes_a_list(self) -> None:
        assert resolve_payload_refs("$payload.people.1", self.PAYLOAD) == "bob"

    def test_an_index_past_the_end_is_none(self) -> None:
        assert resolve_payload_refs("$payload.people.9", self.PAYLOAD) is None

    def test_a_missing_key_is_none(self) -> None:
        assert resolve_payload_refs("$payload.reading.humidity", self.PAYLOAD) is None

    def test_reaching_through_a_scalar_is_none(self) -> None:
        assert resolve_payload_refs("$payload.room.name", self.PAYLOAD) is None

    def test_a_prefix_that_is_not_a_reference_is_left_alone(self) -> None:
        assert resolve_payload_refs("$payloads", self.PAYLOAD) == "$payloads"

    def test_references_are_resolved_inside_nested_containers(self) -> None:
        data = {"brightness": "$payload.reading.temp", "rgb": ["$payload.room", 0]}

        assert resolve_payload_refs(data, self.PAYLOAD) == {
            "brightness": 27.5,
            "rgb": ["nursery", 0],
        }


class TestConditions:
    STATE: ClassVar[dict[str, Any]] = {"state": "above_horizon", "attributes": {"elevation": 12.5}}

    @pytest.mark.parametrize(
        ("attribute", "operator", "value", "expected"),
        [
            ("state", "eq", "above_horizon", True),
            ("state", "ne", "above_horizon", False),
            ("attributes.elevation", "gt", 10, True),
            ("attributes.elevation", "lt", 10, False),
            ("attributes.elevation", "gte", 12.5, True),
            ("attributes.elevation", "lte", 12, False),
        ],
    )
    def test_each_operator(self, attribute: str, operator: str, value: Any, expected: bool) -> None:
        condition = ActuatorCondition("sun.sun", attribute, operator, value)

        assert condition.evaluate(self.STATE) is expected

    def test_an_unknown_operator_does_not_block(self) -> None:
        assert ActuatorCondition("sun.sun", "state", "like", "x").evaluate(self.STATE)

    def test_a_path_through_a_scalar_compares_none(self) -> None:
        condition = ActuatorCondition("sun.sun", "state.inner", "eq", None)

        assert condition.evaluate(self.STATE)

    def test_an_incomparable_value_fails(self) -> None:
        condition = ActuatorCondition("sun.sun", "attributes.missing", "gt", 3)

        assert condition.evaluate(self.STATE) is False


class TestConfigRoundTrip:
    def test_a_full_config_survives_to_dict_and_back(self) -> None:
        config = _config(
            conditions=[ActuatorCondition("sun.sun", "state", "eq", "below_horizon")],
            detection_filter={"person": True},
            actions=[ActuatorAction("light", "turn_on", "light.hall", {"color_name": "red"})],
            cooldown_seconds=30,
        )

        assert ActuatorConfig.from_dict(config.to_dict()) == config

    def test_a_minimal_dict_takes_the_defaults(self) -> None:
        config = ActuatorConfig.from_dict({"automation_id": "a", "mqtt_topics": ["t"]})

        assert config.description == ""
        assert config.actions == []
        assert config.conditions == []
        assert config.detection_filter is None
        assert config.cooldown_seconds == 10.0

    def test_an_action_without_data_gets_an_empty_dict(self) -> None:
        action = ActuatorAction.from_dict({"domain": "d", "service": "s", "entity_id": "e"})

        assert action.service_data == {}


class TestDetectionFilter:
    def test_no_filter_matches_everything(self, tmp_path: Path) -> None:
        assert _agent(tmp_path)._matches_filter({"anything": 1})

    def test_a_literal_must_be_equal(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"label": "person"})

        assert agent._matches_filter({"label": "person"})
        assert not agent._matches_filter({"label": "cat"})

    def test_an_operator_compares(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"confidence": {"gte": 0.8}})

        assert agent._matches_filter({"confidence": 0.9})
        assert not agent._matches_filter({"confidence": 0.5})

    def test_an_operator_on_a_missing_field_does_not_match(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"confidence": {"gt": 0.8}})

        assert not agent._matches_filter({})

    def test_an_unknown_operator_does_not_match(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"confidence": {"about": 0.8}})

        assert not agent._matches_filter({"confidence": 0.8})

    def test_an_incomparable_value_does_not_match(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"confidence": {"gt": 0.8}})

        assert not agent._matches_filter({"confidence": "high"})


class TestDetection:
    async def test_a_matching_detection_calls_every_action(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path,
            actions=[
                ActuatorAction("fan", "turn_on", "fan.nursery"),
                ActuatorAction("light", "turn_on", "$payload.lamp", {"brightness": "$payload.b"}),
            ],
        )
        ha = _HA()
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({"lamp": "light.cot", "b": 40})

        assert ha.calls == [
            ("fan", "turn_on", "fan.nursery", {}),
            ("light", "turn_on", "light.cot", {"brightness": 40}),
        ]
        assert agent._actuations_count == 1
        assert agent.metrics.tasks_completed == 1
        ((topic, record),) = _published(agent)
        assert topic == f"agents/{agent.actor_id}/actuations"
        assert record["trigger_payload"] == {"lamp": "light.cot", "b": 40}

    async def test_an_action_with_an_unresolved_reference_is_skipped(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path,
            actions=[
                ActuatorAction("light", "turn_on", "light.cot", {"brightness": "$payload.b"}),
                ActuatorAction("fan", "turn_on", "fan.nursery"),
            ],
        )
        ha = _HA()
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({"other": 1})

        assert ha.calls == [("fan", "turn_on", "fan.nursery", {})]
        assert agent.metrics.tasks_failed == 1

    async def test_a_filtered_out_detection_does_nothing(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, detection_filter={"label": "person"})
        ha = _HA()
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({"label": "cat"})

        assert ha.calls == []
        assert _published(agent) == []

    async def test_a_second_detection_inside_the_cooldown_is_ignored(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, cooldown_seconds=60)
        ha = _HA()
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({})
        await agent._on_detection({})

        assert len(ha.calls) == 1
        assert agent._actuations_count == 1

    async def test_an_unmet_condition_blocks_the_actions(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path, conditions=[ActuatorCondition("sun.sun", "state", "eq", "below_horizon")]
        )
        ha = _HA({"sun.sun": {"state": "above_horizon"}})
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({})

        assert ha.calls == []
        assert agent._actuations_count == 0

    async def test_a_failed_service_call_is_counted_not_raised(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        ha = _HA()
        ha.fail_calls = True
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        await agent._on_detection({})

        assert agent.metrics.tasks_failed == 1


class TestConditionChecks:
    SUN_DOWN = ActuatorCondition("sun.sun", "state", "eq", "below_horizon")

    async def test_no_conditions_pass(self, tmp_path: Path) -> None:
        assert await _agent(tmp_path)._all_conditions_met()

    async def test_without_a_connection_they_pass(self, tmp_path: Path) -> None:
        assert await _agent(tmp_path, conditions=[self.SUN_DOWN])._all_conditions_met()

    async def test_every_condition_must_hold(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path,
            conditions=[
                self.SUN_DOWN,
                ActuatorCondition("input_boolean.away", "state", "eq", "off"),
            ],
        )
        agent._ha = _HA(  # pyright: ignore[reportAttributeAccessIssue]
            {"sun.sun": {"state": "below_horizon"}, "input_boolean.away": {"state": "on"}}
        )

        assert not await agent._all_conditions_met()

    async def test_all_holding_passes(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, conditions=[self.SUN_DOWN])
        agent._ha = _HA({"sun.sun": {"state": "below_horizon"}})  # pyright: ignore[reportAttributeAccessIssue]

        assert await agent._all_conditions_met()

    async def test_a_missing_entity_fails(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, conditions=[self.SUN_DOWN])
        agent._ha = _HA()  # pyright: ignore[reportAttributeAccessIssue]

        assert not await agent._all_conditions_met()

    async def test_a_lookup_that_raises_fails(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, conditions=[self.SUN_DOWN])
        ha = _HA()
        ha.fail_lookups = True
        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]

        assert not await agent._all_conditions_met()


class TestCallService:
    async def test_a_ready_signal_without_a_client_calls_nothing(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        agent._ws_ready.set()

        await agent._call_service(ActuatorAction("fan", "turn_on", "fan.nursery"))

        assert agent.metrics.tasks_failed == 0

    async def test_a_connection_that_arrives_while_waiting_is_used(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        ha = _HA()
        call = asyncio.create_task(
            agent._call_service(ActuatorAction("fan", "turn_on", "fan.nursery"))
        )
        await asyncio.sleep(0)

        agent._ha = ha  # pyright: ignore[reportAttributeAccessIssue]
        agent._ws_ready.set()
        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        done, _ = await asyncio.wait({call}, timeout=5)

        assert call in done
        assert ha.calls == [("fan", "turn_on", "fan.nursery", {})]


class TestLifecycle:
    async def test_counters_survive_a_restart(self, tmp_path: Path) -> None:
        first = _agent(tmp_path)
        first._actuations_count = 4
        first._last_actuation_time = 1234.5
        await first.on_stop()

        second = _agent(tmp_path)
        second.ha_ws_url = ""
        await second._load_persistent_state()  # what `start()` does before `on_start()`
        await second.on_start()
        for task in second._tasks:
            task.cancel()
        await asyncio.wait(second._tasks, timeout=5)

        assert second._actuations_count == 4
        assert second._last_actuation_time == 1234.5
        assert second.recall("config") == second.config.to_dict()
        (manifest,) = [p for t, p in _published(second) if t.endswith("/manifest")]
        assert manifest["capabilities"] == ["ha_actuator"]
        assert manifest["description"] == "Turns the fan on."

    async def test_the_manifest_describes_an_automation_without_a_description(
        self, tmp_path: Path
    ) -> None:
        agent = _agent(tmp_path, description="")
        agent.ha_ws_url = ""

        await agent.on_start()
        for task in agent._tasks:
            task.cancel()
        await asyncio.wait(agent._tasks, timeout=5)

        (manifest,) = [p for t, p in _published(agent) if t.endswith("/manifest")]
        assert manifest["description"] == "Actuator for fan"


class _WS:
    """Stands in for HAWebSocketClient; `on_enter` runs as the connection opens."""

    def __init__(self, agent: HomeAssistantActuatorAgent, *, fail: bool) -> None:
        self._agent = agent
        self._fail = fail

    def __call__(self, _url: str, _token: str) -> "_WS":
        return self

    async def __aenter__(self) -> "_WS":
        # Stopping here lets each loop end on its own state check, with no sleep.
        self._agent.state = ActorState.STOPPED
        if self._fail:
            raise ConnectionError("refused")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class TestWebSocketKeepalive:
    async def test_without_a_url_or_token_it_does_not_connect(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        agent.ha_ws_url = ""

        await agent._ws_keepalive()

        assert not agent._ws_ready.is_set()

    async def test_a_connection_signals_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        agent.ha_ws_url, agent.ha_token = "ws://ha/api/websocket", "token"
        monkeypatch.setattr(module, "HAWebSocketClient", _WS(agent, fail=False))

        await agent._ws_keepalive()

        assert agent._ws_ready.is_set()
        assert agent._ha is None  # released once the actor stopped

    async def test_a_failed_connection_clears_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        agent.ha_ws_url, agent.ha_token = "ws://ha/api/websocket", "token"
        agent._ws_ready.set()
        monkeypatch.setattr(module, "HAWebSocketClient", _WS(agent, fail=True))

        await agent._ws_keepalive()

        assert not agent._ws_ready.is_set()


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _Client:
    """A broker connection delivering `payloads`, then stopping the agent."""

    def __init__(self, agent: HomeAssistantActuatorAgent, payloads: list[bytes]) -> None:
        self._agent = agent
        self._payloads = payloads
        self.subscribed: list[tuple[str, int]] = []
        self.identifier: str | None = None

    def __call__(self, _host: str, _port: int, **kwargs: Any) -> "_Client":
        self.identifier = kwargs.get("identifier")
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    @property
    def messages(self) -> AsyncIterator[_Message]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[_Message]:
        for payload in self._payloads:
            yield _Message(payload)
        self._agent.state = ActorState.STOPPED


class TestMqttListener:
    async def test_each_topic_is_subscribed_and_each_message_handled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, mqtt_topics=["a/b", "c/d"])
        agent.state = ActorState.RUNNING
        client = _Client(agent, [b'{"n": 1}', b"not json", b'{"n": 2}'])
        monkeypatch.setattr(module, "mqtt_client", client)
        seen: list[Any] = []

        async def _record(payload: Any) -> None:
            seen.append(payload)

        monkeypatch.setattr(agent, "_on_detection", _record)

        await agent._mqtt_listener()

        assert [topic for topic, _ in client.subscribed] == ["a/b", "c/d"]
        assert seen == [{"n": 1}, {"n": 2}]
        assert client.identifier is not None
        assert client.identifier.endswith("-actuator")

    async def test_a_message_after_stopping_is_not_handled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        agent.state = ActorState.RUNNING
        client = _Client(agent, [b'{"n": 1}', b'{"n": 2}'])
        monkeypatch.setattr(module, "mqtt_client", client)
        seen: list[Any] = []

        async def _stop_after_first(payload: Any) -> None:
            seen.append(payload)
            agent.state = ActorState.STOPPED

        monkeypatch.setattr(agent, "_on_detection", _stop_after_first)

        await agent._mqtt_listener()

        assert seen == [{"n": 1}]

    async def test_a_refused_connection_while_stopping_ends_the_listener(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        agent.state = ActorState.RUNNING

        def _refuse(_host: str, _port: int, **_kwargs: Any) -> Any:
            agent.state = ActorState.STOPPED
            raise ConnectionRefusedError("no broker")

        monkeypatch.setattr(module, "mqtt_client", _refuse)

        await agent._mqtt_listener()

        assert agent.state == ActorState.STOPPED


class TestStatus:
    def test_the_default_name_comes_from_the_automation(self, tmp_path: Path) -> None:
        agent = HomeAssistantActuatorAgent(
            config=_config(automation_id="x" * 40), persistence_dir=str(tmp_path)
        )

        assert agent.name == "actuator-" + "x" * 20

    def test_an_agent_that_has_fired_says_how_often(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        agent._actuations_count = 3
        agent._last_actuation_time = 0

        spoken = agent._spoken_status()

        assert "fired 3 times" in spoken
        assert "not connected" in spoken

    def test_an_agent_that_fired_once_says_once(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        agent._actuations_count = 1
        agent._ha = _HA()  # pyright: ignore[reportAttributeAccessIssue]

        spoken = agent._spoken_status()

        assert "fired once" in spoken
        assert "Home Assistant is reachable." in spoken

    def test_the_task_description_names_the_topics(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, mqtt_topics=["a", "b"])

        assert agent._current_task_description() == "listening on [a, b] (0 actuations)"

    async def test_only_a_task_gets_a_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        sent: list[Any] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, msg_type))
            return True

        monkeypatch.setattr(agent, "send", _send)

        await agent.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="x"))
        await agent.handle_message(Message(type=MessageType.TASK, sender_id=""))
        await agent.handle_message(Message(type=MessageType.TASK, sender_id="x"))

        assert sent == [("x", MessageType.RESULT)]
