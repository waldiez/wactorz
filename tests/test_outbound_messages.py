"""An agent says something to a person, with a choice, and hears the pick.

The agent publishes one ``agents/<id>/chat`` frame (``notify_user``); the
outbox in main's process hands it to the channel it names; the channel shows
the options as buttons or a numbered list; the pick comes back on
``agents/<id>/answer``. The dashboard is a channel too: the monitor passes the
options through to the browser, and the browser's pick goes out over the
server's broker link.

Everything here runs without a broker: the outbox is driven frame by frame,
the channels are stand-ins that record what they were handed, and the broker
link is a recorder.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import wactorz.interfaces.chat.social as social
import wactorz.interfaces.chat_interfaces as ci
from wactorz.agents.function_agent import agent, spec_of
from wactorz.core.outbound import (
    DASHBOARD,
    MAX_OPTIONS,
    Answer,
    OutboundMessage,
    check_channel,
    check_options,
    numbered,
    pick_option,
)
from wactorz.interfaces.chat.outbound import Outbox, PendingChoices, Sender
from wactorz.interfaces.chat_interfaces import build_social_companions
from wactorz.web import answers, events, mqtt, relay, runtime, ws

# ── the vocabulary ───────────────────────────────────────────────────────────


class TestCheckOptions:
    def test_strips_and_keeps_order(self) -> None:
        assert check_options([" maintenance ", "real problem"]) == ("maintenance", "real problem")

    def test_none_is_none(self) -> None:
        assert check_options(()) == ()

    @pytest.mark.parametrize(
        ("options", "why"),
        [
            ([1], "a string"),
            ([""], "empty"),
            (["  "], "empty"),
            (["x" * 61], "longer"),
            (["yes", "Yes"], "twice"),
            ([str(n) for n in range(MAX_OPTIONS + 1)], "more than"),
        ],
    )
    def test_refuses_what_a_channel_cannot_show(self, options: list[Any], why: str) -> None:
        with pytest.raises(ValueError, match=why):
            check_options(options)


class TestCheckChannel:
    def test_names_are_case_insensitive_and_empty_means_the_dashboard(self) -> None:
        assert check_channel("Telegram") == "telegram"
        assert check_channel("") == DASHBOARD

    def test_an_unknown_channel_is_refused_with_the_list(self) -> None:
        with pytest.raises(ValueError, match="telegram"):
            check_channel("pager")


class TestNumberedChoices:
    def test_a_numbered_list_follows_the_text(self) -> None:
        text = numbered("Changed?", ["yes", "no"])
        assert text.startswith("Changed?\n\n1. yes\n2. no\n")
        assert text.endswith("Reply with the number.")
        assert numbered("plain", []) == "plain"

    def test_without_the_hint_the_list_is_only_a_record(self) -> None:
        assert numbered("Changed?", ["yes", "no"], reply_hint=False) == "Changed?\n\n1. yes\n2. no"

    @pytest.mark.parametrize(
        ("reply", "index"),
        [
            ("2", 1),
            (" 2.", 1),
            ("(1)", 0),
            ("( 2 )", 1),
            ("\t 1) \n", 0),
            ("1 2", None),
            ("no", 1),
            ("YES", 0),
            ("3", None),
            ("0", None),
            ("", None),
            ("maybe", None),
        ],
    )
    def test_a_reply_picks_by_number_or_by_words(self, reply: str, index: int | None) -> None:
        assert pick_option(reply, ["yes", "no"]) == index

    def test_a_long_run_of_spaces_is_read_at_once(self) -> None:
        # A pattern with repeats side by side takes seconds over this many; the
        # bound is far above what a linear match needs, even on a loaded runner.
        reply = "\t" * 40_000 + "x"

        started = time.perf_counter()
        picked = pick_option(reply, ["yes", "no"])

        assert picked is None
        assert time.perf_counter() - started < 1.0

    def test_nothing_to_pick_from(self) -> None:
        assert pick_option("1", []) is None


class TestTheFrames:
    def test_a_chat_frame_to_the_user_is_a_message(self) -> None:
        frame = {
            "id": "m1",
            "from": "hvac",
            "to": "user",
            "content": "Changed?",
            "options": ["yes", "no"],
            "channel": "Telegram",
            "user": " 42 ",
            "timestamp": 5.0,
        }
        message = OutboundMessage.from_chat_frame("agent-1", frame)
        assert message is not None
        assert message == OutboundMessage(
            id="m1",
            agent_id="agent-1",
            sender="hvac",
            text="Changed?",
            options=("yes", "no"),
            channel="telegram",
            user="42",
            timestamp=5.0,
        )
        assert message.wants_delivery

    def test_a_plain_notification_needs_no_delivery(self) -> None:
        message = OutboundMessage.from_chat_frame("a", {"from": "x", "content": "hi"})
        assert message is not None
        assert not message.wants_delivery
        assert message.options == ()

    @pytest.mark.parametrize(
        "frame",
        [
            "not a dict",
            {"to": "other-agent", "content": "x"},
            {"content": "  "},
            {"options": ["a"]},
        ],
    )
    def test_what_is_not_a_message_to_a_person(self, frame: Any) -> None:
        assert OutboundMessage.from_chat_frame("a", frame) is None

    def test_an_answer_goes_out_and_comes_back_the_same(self) -> None:
        answer = Answer(
            id="m1",
            agent_id="agent-1",
            option="no",
            index=1,
            channel="telegram",
            user="42",
            timestamp=7.0,
        )
        assert answer.topic == "agents/agent-1/answer"
        assert Answer.from_payload("agent-1", answer.to_payload()) == answer
        assert Answer.from_payload("agent-1", {"option": ""}) is None
        assert Answer.from_payload("agent-1", {"option": "x", "index": "many"}) is None
        assert Answer.from_payload("agent-1", "nope") is None


# ── the choices a channel remembers ──────────────────────────────────────────


def _question(
    user: str = "42", options: tuple[str, ...] = ("yes", "no"), **over: Any
) -> OutboundMessage:
    fields: dict[str, Any] = {
        "id": "m1",
        "agent_id": "agent-1",
        "sender": "hvac",
        "text": "Changed?",
        "options": options,
        "channel": "discord",
        "user": user,
    }
    fields.update(over)
    return OutboundMessage(**fields)


class TestPendingChoices:
    def test_a_numbered_reply_answers_the_latest_question(self) -> None:
        choices = PendingChoices()
        choices.remember(_question(id="old"))
        choices.remember(_question(id="new", options=("a", "b", "c")))
        answer = choices.match("42", "3")
        assert answer is not None
        assert (answer.id, answer.option, answer.index, answer.channel, answer.user) == (
            "new",
            "c",
            2,
            "discord",
            "42",
        )
        # Answered: the next reply from that person is a message again.
        assert choices.match("42", "1") is None
        assert len(choices) == 0

    def test_a_reply_that_picks_nothing_leaves_the_question_open(self) -> None:
        choices = PendingChoices()
        choices.remember(_question())
        assert choices.match("42", "what do you mean?") is None
        assert choices.match("other", "1") is None
        assert len(choices) == 1

    def test_only_a_question_with_options_and_a_person_is_kept(self) -> None:
        choices = PendingChoices()
        choices.remember(_question(options=()))
        choices.remember(_question(user=""))
        assert len(choices) == 0


# ── the outbox ───────────────────────────────────────────────────────────────


class _Channel:
    """A channel that records what it was asked to send, or refuses to."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    async def send(
        self, user: str, text: str, options=(), *, message_id: str = "", agent_id: str = ""
    ) -> str:
        if self.fail:
            raise RuntimeError("the network is down")
        self.sent.append(
            {
                "user": user,
                "text": text,
                "options": tuple(options),
                "id": message_id,
                "agent_id": agent_id,
            }
        )
        return message_id


class _Broker:
    """The broker link an outbox publishes answers through."""

    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any], int]] = []
        self.fail = fail

    async def publish(self, topic: str, payload: str, qos: int = 0, **_kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("broker gone")
        self.published.append((topic, json.loads(payload), qos))


def _telegram_frame(**over: Any) -> str:
    frame: dict[str, Any] = {
        "id": "m1",
        "from": "hvac",
        "to": "user",
        "content": "Changed?",
        "options": ["yes", "no"],
        "channel": "telegram",
        "user": "42",
    }
    frame.update(over)
    return json.dumps(frame)


class TestTheOutbox:
    async def test_a_frame_for_a_running_channel_is_handed_to_it(self) -> None:
        outbox = Outbox()
        telegram = _Channel()
        outbox.register("Telegram", cast(Sender, telegram))
        assert outbox.channels() == frozenset({"telegram"})

        assert await outbox.handle_frame("agents/agent-1/chat", _telegram_frame())

        assert telegram.sent == [
            {
                "user": "42",
                "text": "Changed?",
                "options": ("yes", "no"),
                "id": "m1",
                "agent_id": "agent-1",
            }
        ]

    async def test_a_frame_for_the_dashboard_needs_no_delivery(self) -> None:
        outbox = Outbox()
        telegram = _Channel()
        outbox.register("telegram", cast(Sender, telegram))
        assert not await outbox.handle_frame("agents/a/chat", _telegram_frame(channel="dashboard"))
        assert not await outbox.handle_frame(
            "agents/a/chat", json.dumps({"from": "x", "content": "hi"})
        )
        assert telegram.sent == []

    async def test_a_channel_that_is_not_running_is_named_with_the_words(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        outbox = Outbox()
        outbox.register("discord", cast(Sender, _Channel()))
        with caplog.at_level(logging.WARNING):
            assert not await outbox.handle_frame("agents/a/chat", _telegram_frame())
        assert "telegram" in caplog.text
        assert "running: discord" in caplog.text
        assert "Changed?" in caplog.text

    async def test_nobody_to_reach_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        outbox = Outbox()
        outbox.register("telegram", cast(Sender, _Channel()))
        with caplog.at_level(logging.WARNING):
            assert not await outbox.handle_frame("agents/a/chat", _telegram_frame(user=""))
        assert "without saying who to reach" in caplog.text

    async def test_a_failing_send_is_logged_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        outbox = Outbox()
        outbox.register("telegram", cast(Sender, _Channel(fail=True)))
        with caplog.at_level(logging.ERROR):
            assert not await outbox.handle_frame("agents/a/chat", _telegram_frame())
        assert "could not reach '42' on telegram" in caplog.text

    async def test_a_channel_that_went_away_is_forgotten(self) -> None:
        outbox = Outbox()
        outbox.register("telegram", cast(Sender, _Channel()))
        outbox.unregister("telegram")
        outbox.unregister("telegram")  # twice is fine
        assert outbox.channels() == frozenset()

    @pytest.mark.parametrize(
        ("topic", "payload"),
        [
            ("agents/a/status", "{}"),
            ("agents/a/chat/extra", _telegram_frame()),
            ("nodes/a/chat", _telegram_frame()),
            ("agents/a/chat", "not json"),
        ],
    )
    async def test_other_frames_are_ignored(self, topic: str, payload: str) -> None:
        outbox = Outbox()
        outbox.register("telegram", cast(Sender, _Channel()))
        assert not await outbox.handle_frame(topic, payload)

    async def test_an_answer_is_published_for_the_agent(self) -> None:
        outbox = Outbox()
        broker = _Broker()
        outbox._client = broker
        answer = Answer(
            id="m1", agent_id="agent-1", option="no", index=1, channel="telegram", user="42"
        )

        assert await outbox.answered(answer)

        ((topic, payload, qos),) = broker.published
        assert topic == "agents/agent-1/answer"
        assert qos == 1
        assert {k: payload[k] for k in ("id", "option", "index", "channel", "user", "from")} == {
            "id": "m1",
            "option": "no",
            "index": 1,
            "channel": "telegram",
            "user": "42",
            "from": "user",
        }

    async def test_an_answer_with_no_broker_or_no_agent_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        outbox = Outbox()
        answer = Answer(
            id="m1", agent_id="agent-1", option="no", index=1, channel="telegram", user="42"
        )
        with caplog.at_level(logging.INFO):
            assert not await outbox.answered(answer)
            assert not await outbox.answered(
                Answer(id="m1", agent_id="", option="no", index=1, channel="x")
            )
        assert "no broker link" in caplog.text
        assert "no agent sent" in caplog.text

    async def test_a_failed_publish_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        outbox = Outbox()
        outbox._client = _Broker(fail=True)
        answer = Answer(
            id="m1", agent_id="agent-1", option="no", index=1, channel="telegram", user="42"
        )
        with caplog.at_level(logging.ERROR):
            assert not await outbox.answered(answer)
        assert "could not publish an answer" in caplog.text


# ── the agent's call ─────────────────────────────────────────────────────────


@agent(name="hvac-watch")
def hvac_watch(payload: dict) -> dict:
    return payload


class TestNotifyUser:
    @pytest.fixture(name="published")
    def published_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Any, list]:
        spec = spec_of(hvac_watch)
        assert spec is not None
        actor = spec.build(persistence_dir=str(tmp_path))
        published: list[tuple[str, dict[str, Any], int]] = []

        async def _record(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            published.append((topic, payload, qos))

        monkeypatch.setattr(actor, "_mqtt_publish", _record)
        return actor, published

    async def test_a_question_carries_its_options_and_where_to_send_it(
        self, published: tuple[Any, list]
    ) -> None:
        actor, frames = published
        message_id = await actor.notify_user(
            "Changed?", options=["yes", " no "], channel="Telegram", user=" 42 "
        )
        ((topic, payload, qos),) = frames
        assert topic == f"agents/{actor.actor_id}/chat"
        assert qos == 1
        assert payload["id"] == message_id
        assert len(message_id) == 32
        assert payload["from"] == "hvac-watch"
        assert payload["to"] == "user"
        assert payload["content"] == "Changed?"
        assert payload["options"] == ["yes", "no"]
        assert payload["channel"] == "telegram"
        assert payload["user"] == "42"

    async def test_a_plain_notification_reads_as_before(self, published: tuple[Any, list]) -> None:
        actor, frames = published
        await actor.notify_user("done", extra_field=1)
        ((_topic, payload, _qos),) = frames
        assert "options" not in payload
        assert "channel" not in payload
        assert "user" not in payload
        assert payload["extra_field"] == 1
        assert payload["id"]

    async def test_a_bad_choice_is_refused_before_anything_goes_out(
        self, published: tuple[Any, list]
    ) -> None:
        actor, frames = published
        with pytest.raises(ValueError):
            await actor.notify_user("Changed?", options=["yes", "yes"])
        with pytest.raises(ValueError):
            await actor.notify_user("Changed?", channel="pager")
        assert frames == []


# ── the dashboard as a channel ───────────────────────────────────────────────


@pytest.fixture(name="quiet_monitor")
def quiet_monitor_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A monitor with nothing attached: broadcasts are recorded, the feed is fresh."""
    monkeypatch.setattr(
        runtime,
        "state",
        {
            "agents": {"agent-1": {"name": "hvac"}},
            "nodes": {},
            "alerts": [],
            "system_health": {},
            "log_feed": [],
        },
    )
    monkeypatch.setattr(runtime, "mqtt_client_ref", None)
    monkeypatch.setattr(runtime, "db", None)
    monkeypatch.setattr(runtime, "hard_resetting", False)
    monkeypatch.setattr(events, "snapshot", lambda include_totals=True: {})
    sent: list[dict[str, Any]] = []

    async def _broadcast(msg: dict[str, Any]) -> None:
        sent.append(msg)

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return sent


class TestTheDashboardShowsTheQuestion:
    def test_the_options_and_the_other_channel_reach_the_browser(self, quiet_monitor: list) -> None:
        event = events.parse_topic("agents/agent-1/chat", _telegram_frame())
        assert event is not None
        push = event["_push_chat"]
        assert push["id"] == "m1"
        assert push["options"] == ["yes", "no"]
        assert push["channel"] == "telegram"
        entry = runtime.state["log_feed"][-1]
        assert entry["options"] == ["yes", "no"]

    def test_a_plain_frame_carries_none_of_it(self, quiet_monitor: list) -> None:
        event = events.parse_topic(
            "agents/agent-1/chat", json.dumps({"from": "hvac", "content": "hi"})
        )
        assert event is not None
        assert "options" not in event["_push_chat"]
        assert "channel" not in event["_push_chat"]
        # A frame that names the dashboard says nothing the dashboard needs.
        event = events.parse_topic("agents/agent-1/chat", _telegram_frame(channel="dashboard"))
        assert event is not None
        assert "channel" not in event["_push_chat"]

    def test_an_answer_goes_on_the_feed_and_to_the_browsers(self, quiet_monitor: list) -> None:
        answer = Answer(
            id="m1", agent_id="agent-1", option="no", index=1, channel="telegram", user="42"
        )
        event = events.parse_topic(answer.topic, json.dumps(answer.to_payload()))
        assert event is not None
        entry = runtime.state["log_feed"][-1]
        assert (entry["type"], entry["name"], entry["option"], entry["channel"]) == (
            "answer",
            "hvac",
            "no",
            "telegram",
        )
        assert relay.is_relayed(answer.topic)

    def test_a_frame_that_is_no_answer_leaves_the_feed_alone(self, quiet_monitor: list) -> None:
        events.parse_topic("agents/agent-1/answer", json.dumps({"index": 0}))
        assert runtime.state["log_feed"] == []

    async def test_a_question_is_stored_with_its_options_and_no_reply_hint(
        self, quiet_monitor: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows: list[dict[str, Any]] = []

        class _DB:
            def write_chat_log(self, **row: Any) -> None:
                rows.append(row)

        monkeypatch.setattr(runtime, "db", _DB())
        await mqtt.handle_message("agents/agent-1/chat", _telegram_frame())
        (row,) = rows
        assert row["agent_name"] == "hvac"
        assert row["role"] == "assistant"
        # The dashboard answers with a button: "reply with the number" would be wrong there.
        assert row["content"] == "Changed?\n\n1. yes\n2. no"


class TestTheDashboardAnswers:
    def test_a_good_frame_becomes_an_answer(self) -> None:
        answer = answers.answer_from_frame(
            {"agent_id": "agent-1", "id": "m1", "option": " no ", "index": 1}
        )
        assert isinstance(answer, Answer)
        assert (answer.agent_id, answer.id, answer.option, answer.index, answer.channel) == (
            "agent-1",
            "m1",
            "no",
            1,
            "dashboard",
        )

    @pytest.mark.parametrize(
        ("frame", "why"),
        [
            ({"id": "m1", "option": "no"}, "names the agent"),
            ({"agent_id": "a/b", "id": "m1", "option": "no"}, "names the agent"),
            ({"agent_id": "a+", "id": "m1", "option": "no"}, "names the agent"),
            ({"agent_id": "a", "option": "no"}, "names the message"),
            ({"agent_id": "a", "id": "m1", "option": ""}, "not an option"),
            ({"agent_id": "a", "id": "m1", "option": "x" * 61}, "not an option"),
            ({"agent_id": "a", "id": "m1", "option": "no", "index": "two"}, "a number"),
            ({"agent_id": "a", "id": "m1", "option": "no", "index": -1}, "a number"),
        ],
    )
    def test_a_bad_frame_says_what_is_wrong(self, frame: dict[str, Any], why: str) -> None:
        problem = answers.answer_from_frame(frame)
        assert isinstance(problem, str)
        assert why in problem

    async def test_the_pick_goes_out_on_the_agents_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker()
        monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
        problem = await answers.answer_from_dashboard(
            {"type": "answer", "agent_id": "agent-1", "id": "m1", "option": "no", "index": 1}
        )
        assert problem is None
        ((topic, payload, qos),) = broker.published
        assert (topic, qos, payload["option"], payload["channel"]) == (
            "agents/agent-1/answer",
            1,
            "no",
            "dashboard",
        )

    async def test_without_a_broker_the_person_is_told(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "mqtt_client_ref", None)
        problem = await answers.answer_from_dashboard({"agent_id": "a", "id": "m1", "option": "no"})
        assert problem is not None
        assert "broker" in problem

    async def test_a_failed_publish_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "mqtt_client_ref", _Broker(fail=True))
        problem = await answers.answer_from_dashboard({"agent_id": "a", "id": "m1", "option": "no"})
        assert problem is not None
        assert "could not be sent" in problem

    async def test_a_bad_frame_is_refused_before_the_broker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker()
        monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
        assert await answers.answer_from_dashboard({"option": "no"})
        assert broker.published == []


# ── the companions share the outbox ──────────────────────────────────────────


def test_the_companions_are_given_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci,
        "CONFIG",
        SimpleNamespace(
            discord_token="d",
            telegram_token="t",
            discord_allowed_user_ids=frozenset({7}),
            telegram_allowed_user_ids=frozenset({42}),
            social_rate_limit_per_min=12,
        ),
    )
    monkeypatch.setattr(social, "missing_dependency", lambda channel: None)
    outbox = Outbox()

    class _Orchestrator:
        async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
            return ""

        def handle_turn_stream(self, text: str, *, channel: str, user=None, attachments=None):
            raise NotImplementedError

        def commands(self) -> frozenset[str]:
            return frozenset()

    companions = build_social_companions(cast(Any, _Orchestrator()), "rest", outbox=outbox)
    assert len(companions) == 2
    assert all(c.outbox is outbox for c in companions)
