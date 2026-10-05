"""A rule acts when a message on its trigger topic satisfies its conditions.

The glue of a pipeline without a program per rule: conditions on the payload,
a cooldown, and actions that publish, send a task or call a webhook. A Home
Assistant service call stays with the `ha_actuator` type.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.rule_agent import (
    RuleAction,
    RuleAgent,
    RuleCondition,
    RuleConfig,
    field_value,
)
from wactorz.core.actor import Message, MessageType


class FakeRegistry:
    def __init__(self, *actors: Any) -> None:
        self._by_name = {a.name: a for a in actors}
        self.delivered: list[tuple[str, Message]] = []

    def find_by_name(self, name: str) -> Any:
        return self._by_name.get(name)

    async def deliver(self, target_id: str, msg: Message) -> bool:
        self.delivered.append((target_id, msg))
        return True


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


@pytest.fixture(name="published")
def published_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        seen.append((topic, payload))

    monkeypatch.setattr(RuleAgent, "_mqtt_publish", record)
    return seen


def _rule(tmp_path: Path, **raw: Any) -> RuleAgent:
    raw.setdefault("triggers", ["anomalies/imu"])
    raw.setdefault("actions", [{"type": "publish", "topic": "alerts/imu"}])
    return RuleAgent(RuleConfig.from_dict(raw), name="imu-alert", persistence_dir=str(tmp_path))


class TestConditions:
    @pytest.mark.parametrize(
        ("op", "value", "payload", "expected"),
        [
            ("gt", 10, {"score": 12}, True),
            (">", 10, {"score": 9}, False),
            ("eq", "walking", {"label": "walking"}, True),
            ("!=", "walking", {"label": "walking"}, False),
            ("in", ["a", "b"], {"label": "b"}, True),
            ("contains", "arm", {"label": "alarm"}, True),
            ("gte", 1, {"score": "high"}, False),
        ],
    )
    def test_operators(self, op: str, value: Any, payload: dict, expected: bool) -> None:
        field = next(iter(payload))
        condition = RuleCondition.from_dict({"field": field, "op": op, "value": value})

        assert condition.holds(payload) is expected

    def test_dotted_fields_and_presence(self) -> None:
        payload = {"reading": {"ax": 9.0, "parts": [1, 2]}}

        assert field_value(payload, "reading.ax") == (True, 9.0)
        assert field_value(payload, "reading.parts.1") == (True, 2)
        assert field_value(payload, "reading.missing") == (False, None)
        assert RuleCondition("reading.ax", "exists").holds(payload)
        assert RuleCondition("reading.missing", "absent").holds(payload)
        assert not RuleCondition("reading.missing", "gt", 1).holds(payload)

    def test_an_unknown_operator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="operator"):
            RuleCondition.from_dict({"field": "x", "op": "near", "value": 1})


class TestConfig:
    def test_a_rule_needs_a_trigger_and_an_action(self) -> None:
        with pytest.raises(ValueError, match="trigger"):
            RuleConfig.from_dict({"actions": [{"type": "publish", "topic": "t"}]})
        with pytest.raises(ValueError, match="action"):
            RuleConfig.from_dict({"triggers": "a/b"})

    @pytest.mark.parametrize(
        "raw",
        [
            {"type": "publish"},
            {"type": "task"},
            {"type": "webhook", "url": "ftp://x"},
            {"type": "email"},
        ],
    )
    def test_an_incomplete_action_is_refused(self, raw: dict) -> None:
        with pytest.raises(ValueError):
            RuleAction.from_dict(raw)

    def test_round_trip(self) -> None:
        raw = {
            "triggers": ["a/b"],
            "conditions": [{"field": "score", "op": ">", "value": 1}],
            "actions": [{"type": "webhook", "url": "https://h/x", "include_trigger": False}],
            "cooldown_seconds": 5,
            "description": "d",
        }

        config = RuleConfig.from_dict(raw)

        assert RuleConfig.from_dict(config.to_dict()) == config
        assert config.publishes == ()

    def test_payload_placeholders_are_filled_from_the_trigger(self) -> None:
        action = RuleAction.from_dict(
            {"type": "task", "agent": "notify", "payload": {"text": "score {score} on {where}"}}
        )

        body = action.body({"score": 12.5})

        assert body["text"] == "score 12.5 on {where}"
        assert body["trigger"] == {"score": 12.5}


class TestPlaceholders:
    """An action's placeholders read the trigger the way a condition's ``field`` does."""

    def _text(self, template: str, trigger: dict[str, Any]) -> str:
        action = RuleAction.from_dict(
            {"type": "publish", "topic": "t", "payload": {"text": template}}
        )
        return action.body(trigger)["text"]

    def test_a_dotted_path_reaches_into_the_payload(self) -> None:
        trigger = {"reading": {"score": 12.5, "axes": [1, 2]}}

        assert self._text("score {reading.score}", trigger) == "score 12.5"
        assert self._text("second {reading.axes.1}", trigger) == "second 2"

    def test_a_format_spec_applies_to_the_value(self) -> None:
        assert self._text("{reading.score:.1f}", {"reading": {"score": 12.345}}) == "12.3"

    @pytest.mark.parametrize(
        "template",
        ["{reading.missing}", "{nowhere.at.all}", "{a[missing]}", "{reading.score.deeper}"],
    )
    def test_a_path_the_trigger_lacks_is_sent_as_written(self, template: str) -> None:
        trigger = {"reading": {"score": 1}, "a": {}}

        assert self._text(f"x {template} y", trigger) == f"x {template} y"

    def test_known_and_unknown_fill_side_by_side(self) -> None:
        trigger = {"reading": {"score": 3}}

        assert self._text("{reading.score} of {limit}", trigger) == "3 of {limit}"

    def test_a_string_that_cannot_render_is_sent_unchanged(self) -> None:
        trigger = {"name": "pump"}

        assert self._text("{name:.2f}", trigger) == "{name:.2f}"
        assert self._text("open { brace", trigger) == "open { brace"


class TestEvaluation:
    async def test_it_fires_when_the_conditions_hold(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        rule = _rule(tmp_path, conditions=[{"field": "score", "op": "gt", "value": 10}])

        assert not await rule.evaluate({"score": 3})
        assert await rule.evaluate({"score": 30})

        data = [(t, p) for t, p in published if not t.startswith("agents/")]
        assert data == [("alerts/imu", {"trigger": {"score": 30}})]
        assert rule.fired == 1
        assert rule.metrics.messages_processed == 2
        assert rule.metrics.tasks_completed == 1
        assert any(t.endswith("/logs") for t, _ in published)

    async def test_the_cooldown_holds_a_second_firing(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        rule = _rule(tmp_path, cooldown_seconds=60)

        assert await rule.evaluate({"score": 1})
        assert not await rule.evaluate({"score": 2})

        assert sum(1 for t, _ in published if t == "alerts/imu") == 1

    async def test_a_task_action_reaches_the_named_agent(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        rule = _rule(
            tmp_path,
            actions=[{"type": "task", "agent": "notify", "payload": {"text": "hi {label}"}}],
        )
        registry = FakeRegistry(_Agent("notify"))
        rule._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        assert await rule.evaluate({"label": "x"})

        target, msg = registry.delivered[0]
        assert target == "notify-id"
        assert msg.type == MessageType.TASK
        assert msg.payload == {"text": "hi x", "trigger": {"label": "x"}}

    async def test_a_missing_task_agent_is_logged_not_fatal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        rule = _rule(tmp_path, actions=[{"type": "task", "agent": "nobody"}])
        rule._registry = FakeRegistry()  # type: ignore[assignment]  # stands in for ActorRegistry

        assert await rule.evaluate({})
        assert "nobody" in caplog.text

    async def test_a_failing_action_does_not_stop_the_others(
        self, tmp_path: Path, published: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rule = _rule(
            tmp_path,
            actions=[
                {"type": "webhook", "url": "https://hooks.example/x"},
                {"type": "publish", "topic": "alerts/imu"},
            ],
        )

        async def refuse(self: Any, action: Any, body: Any) -> None:
            raise RuntimeError("no network")

        monkeypatch.setattr(RuleAgent, "_call_webhook", refuse)

        assert await rule.evaluate({})

        assert any(t == "alerts/imu" for t, _ in published)
        assert rule.metrics.tasks_failed == 1

    async def test_a_task_is_a_trial_run_that_answers_the_verdict(
        self, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        rule = _rule(tmp_path, conditions=[{"field": "score", "op": "gt", "value": 10}])
        registry = FakeRegistry()
        rule._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry

        await rule.handle_message(
            Message(
                type=MessageType.TASK, sender_id="main-id", payload={"_task_id": "t1", "score": 50}
            )
        )

        _target, reply = registry.delivered[0]
        assert reply.type == MessageType.RESULT
        assert reply.payload == {"fired": True, "rule": "imu-alert", "_task_id": "t1"}

    async def test_on_start_subscribes_to_each_trigger(
        self, tmp_path: Path, published: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rule = _rule(tmp_path, triggers=["a/one", "b/#"])
        subscribed: list[str] = []
        monkeypatch.setattr(rule, "subscribe", lambda topic, cb: subscribed.append(topic))

        await rule.on_start()

        assert subscribed == ["a/one", "b/#"]
        manifest = next(p for t, p in published if t.endswith("/manifest"))
        assert manifest["publishes"] == ["alerts/imu"]
        assert manifest["subscribes"] == ["a/one", "b/#"]
