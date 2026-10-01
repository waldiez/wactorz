"""A full mailbox holds its sender for a bounded time, and a reporter not at all.

An actor reads its mailbox one message at a time, and a message can take
minutes when it ends in an LLM call. Whatever writes to that mailbox meanwhile
is usually another actor in the middle of a message of its own, so a write that
waits for room without limit passes the stall on: to the sender, to whatever
writes to the sender, and to the supervisor reporting a restart to a main that
is busy.

So a mailbox with no room refuses. A notification is dropped at once, anything
else is given a bounded wait first, and the sender is told either way.

The supervisor's own reports are the exception to "dropped": it keeps the ones
main had no room for and hands them over at a later check.

The SDK clients' own retries are part of the same arithmetic: they sit under
the retry policy every provider already inherits, and the two multiply the time
one message can hold an actor.
"""

import asyncio
import time
from typing import Any

import pytest

from wactorz.core import actor as actor_module
from wactorz.core import registry as registry_module
from wactorz.core.actor import Actor, Message, MessageType
from wactorz.core.registry import ActorRegistry, Supervisor

#: Long enough to tell a wait from no wait, short enough to run in a test.
WAIT_S = 0.3


class _Idle(Actor):
    """An actor whose message loop is never started, so its mailbox only fills."""

    async def handle_message(self, msg: Message) -> None:
        return None


def _task(text: str = "work") -> Message:
    return Message(type=MessageType.TASK, sender_id="sender-id", payload={"text": text})


def _alert() -> Message:
    payload = {"_monitor_notification": True, "message": "an agent restarted"}
    return Message(type=MessageType.TASK, sender_id="sender-id", payload=payload)


async def _full(name: str = "busy", size: int = 2) -> _Idle:
    actor = _Idle(name=name, mailbox_size=size)
    for _ in range(size):
        assert await actor.receive(_task())
    return actor


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actor_module, "MAILBOX_WAIT_S", WAIT_S)


class TestAMailboxWithRoom:
    async def test_it_takes_the_message_at_once(self) -> None:
        actor = _Idle(name="idle", mailbox_size=2)

        started = time.monotonic()
        taken = await actor.receive(_task())

        assert taken is True
        assert time.monotonic() - started < WAIT_S
        assert actor.metrics.messages_refused == 0


class TestAFullMailbox:
    @pytest.mark.parametrize(
        "message",
        [
            Message(type=MessageType.HEARTBEAT, sender_id="s"),
            Message(type=MessageType.STATUS_REQUEST, sender_id="s"),
            Message(type=MessageType.STATUS_RESPONSE, sender_id="s", payload={"state": "ok"}),
            Message(type=MessageType.TICK, sender_id="s"),
            _alert(),
        ],
        ids=["heartbeat", "status request", "status response", "tick", "alert for main"],
    )
    async def test_a_notification_is_dropped_without_waiting(self, message: Message) -> None:
        actor = await _full()

        started = time.monotonic()
        taken = await actor.receive(message)

        assert taken is False
        assert time.monotonic() - started < WAIT_S / 2
        assert actor.metrics.messages_refused == 1

    @pytest.mark.parametrize(
        "kind",
        [MessageType.TASK, MessageType.RESULT, MessageType.STOP, MessageType.SPAWN],
    )
    async def test_anything_else_waits_and_is_then_refused(self, kind: MessageType) -> None:
        actor = await _full()

        started = time.monotonic()
        taken = await actor.receive(Message(type=kind, sender_id="s", payload={}))

        assert taken is False
        assert time.monotonic() - started >= WAIT_S
        assert actor.metrics.messages_refused == 1
        assert actor._mailbox.qsize() == 2, "nothing was pushed out to make room"

    async def test_room_that_appears_during_the_wait_is_taken(self) -> None:
        actor = await _full()

        async def _read_one() -> None:
            await asyncio.sleep(WAIT_S / 3)
            actor._mailbox.get_nowait()

        reading = asyncio.create_task(_read_one())
        taken = await actor.receive(_task("the one that waited"))
        await reading

        assert taken is True
        assert actor.metrics.messages_refused == 0

    async def test_the_first_refusal_is_logged_with_who_and_what(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        actor = await _full(name="planner")

        await actor.receive(_alert())

        assert "[planner] Mailbox full at 2" in caplog.text
        assert "not keeping up" in caplog.text


class TestTheSenderIsTold:
    async def test_a_delivery_reports_the_refusal(self) -> None:
        registry = ActorRegistry()
        actor = await _full()
        await registry.register(actor)

        assert await registry.deliver(actor.actor_id, _alert()) is False

    async def test_send_passes_it_on(self) -> None:
        registry = ActorRegistry()
        busy, sender = await _full(), _Idle(name="sender")
        await registry.register(busy)
        await registry.register(sender)
        sender._registry = registry

        assert await sender.send(busy.actor_id, MessageType.HEARTBEAT) is False
        assert await sender.send(sender.actor_id, MessageType.HEARTBEAT) is True

    async def test_a_broadcast_waits_once_however_many_are_full(self) -> None:
        registry = ActorRegistry()
        crowd = [await _full(name=f"busy-{index}") for index in range(4)]
        listener = _Idle(name="listener")
        for actor in (*crowd, listener):
            await registry.register(actor)

        started = time.monotonic()
        await registry.broadcast("someone-else", MessageType.TASK, {"text": "all of you"})

        assert time.monotonic() - started < WAIT_S * 2
        assert listener._mailbox.qsize() == 1
        assert [actor.metrics.messages_refused for actor in crowd] == [1, 1, 1, 1]


async def _supervising(main: _Idle) -> Supervisor:
    """A supervisor with one running actor to report from, and ``main`` to report to."""
    registry = ActorRegistry()
    worker = _Idle(name="worker")
    await registry.register(main)
    await registry.register(worker)
    supervisor = Supervisor(registry, lambda _actor: None)
    supervisor.supervise("worker", lambda: worker)
    next(iter(supervisor._specs.values())).actor = worker
    return supervisor


def _reports(main: _Idle) -> list[str]:
    """The supervisor's reports waiting in main's mailbox, in order."""
    waiting = []
    while not main._mailbox.empty():
        payload = main._mailbox.get_nowait().payload
        if payload.get("_monitor_notification"):
            waiting.append(payload["message"])
    return waiting


class TestTheSupervisorIsNotHeldUp:
    """Its reports to main follow a restart; held up there, it would stop supervising."""

    async def test_reporting_to_a_main_that_is_full_returns_at_once(self) -> None:
        main = await _full(name="main")
        supervisor = await _supervising(main)

        started = time.monotonic()
        await supervisor._notify_main("worker restarted")

        assert time.monotonic() - started < WAIT_S / 2

    async def test_main_still_hears_of_it_once_it_has_room(self) -> None:
        # Not dropped like another notification: a restart main never hears of
        # is one nobody is told about.
        main = await _full(name="main")
        supervisor = await _supervising(main)
        await supervisor._notify_main("worker restarted")
        await supervisor._notify_main("worker restarted again")
        assert main.metrics.messages_refused == 0, "kept, so not counted as refused"

        _reports(main)  # main reads its mailbox
        supervisor._hand_over_held_reports()

        assert _reports(main) == ["worker restarted", "worker restarted again"]
        assert not supervisor._held_reports

    async def test_a_later_report_does_not_overtake_one_still_held(self) -> None:
        main = await _full(name="main", size=2)
        supervisor = await _supervising(main)
        await supervisor._notify_main("first")
        main._mailbox.get_nowait()  # room for one

        await supervisor._notify_main("second")
        supervisor._hand_over_held_reports()

        _task_left, report = main._mailbox.get_nowait(), main._mailbox.get_nowait()
        assert report.payload["message"] == "first"
        assert [held.payload["message"] for held in supervisor._held_reports] == ["second"]

    async def test_a_main_that_never_reads_cannot_grow_the_list_without_limit(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(registry_module, "HELD_REPORTS", 3)
        main = await _full(name="main")
        supervisor = await _supervising(main)

        for number in range(5):
            await supervisor._notify_main(f"report {number}")

        held = [report.payload["message"] for report in supervisor._held_reports]
        assert held == ["report 2", "report 3", "report 4"]
        assert "dropping the oldest" in caplog.text


class TestTheSdkClientsDoNotRetryOnTheirOwn:
    """`LLMProvider` retries, for every provider alike; the SDK's own would multiply it."""

    def test_anthropic(self) -> None:
        pytest.importorskip("anthropic")
        from wactorz.agents.llm.providers.anthropic import (
            AnthropicProvider,
        )  # an optional dependency

        assert AnthropicProvider(api_key="test-key").client.max_retries == 0

    @pytest.mark.parametrize("base_url", [None, "http://localhost:9/v1"])
    def test_openai_with_and_without_a_base_url(self, base_url: str | None) -> None:
        pytest.importorskip("openai")
        from wactorz.agents.llm.providers.openai import OpenAIProvider  # an optional dependency

        client: Any = OpenAIProvider(api_key="test-key", base_url=base_url).client
        assert client.max_retries == 0

    def test_nim(self) -> None:
        pytest.importorskip("openai")
        from wactorz.agents.llm.providers.nim import NIMProvider  # an optional dependency

        client: Any = NIMProvider(api_key="test-key").client
        assert client.max_retries == 0
