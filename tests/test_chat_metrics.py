"""How long a person waits in the chat, from the server's side.

Every message from the dashboard is routed through `route_chat`, and every
route ends by sending the reply, so a turn is timed there: until its first
words, and until it is over, labelled by where it went. A turn the person
stops is not recorded.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from wactorz.core.actor import ActorState
from wactorz.monitoring import chat_metrics
from wactorz.monitoring.chat_metrics import TurnTimer
from wactorz.web import chat, runtime


def _count(name: str, kind: str) -> float:
    """How many turns of ``kind`` the histogram ``name`` holds."""
    registry = CollectorRegistry()
    for collector in chat_metrics.COLLECTORS:
        registry.register(collector)
    return registry.get_sample_value(f"{name}_count", {"kind": kind}) or 0.0


def _turns(kind: str) -> float:
    return _count("wactorz_chat_turn_duration_seconds", kind)


def _first_replies(kind: str) -> float:
    return _count("wactorz_chat_first_reply_seconds", kind)


class _Agent:
    """A local agent answering through `process_user_input`."""

    def __init__(self, name: str, answer: Any = "hello there") -> None:
        self.name = name
        self.state = ActorState.RUNNING
        self._answer = answer

    async def process_user_input(self, text: str) -> str:
        if isinstance(self._answer, asyncio.Event):
            await self._answer.wait()
        return str(self._answer)


class _Registry:
    def __init__(self, *agents: _Agent) -> None:
        self._agents = {a.name: a for a in agents}

    def find_by_name(self, name: str) -> Any:
        return self._agents.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._agents.values())


@pytest.fixture(autouse=True)
def _no_registry() -> Iterator[None]:
    saved = runtime.registry
    runtime.registry = None
    yield
    runtime.registry = saved


async def _replies(_text: str) -> None:
    return None


class TestTheTimer:
    async def test_the_first_reply_is_when_the_first_words_are_sent(self) -> None:
        kind = "timer-first"
        timer = TurnTimer(kind)
        send = timer.watch(_replies)

        await send("first")
        await send("second")
        timer.finish()

        assert _first_replies(kind) == 1
        assert _turns(kind) == 1

    async def test_a_turn_that_said_nothing_has_no_first_reply(self) -> None:
        kind = "timer-silent"
        TurnTimer(kind).finish()

        assert _first_replies(kind) == 0
        assert _turns(kind) == 1


class TestWhereATurnWent:
    def test_a_slash_command(self) -> None:
        assert chat.destination_of("/nodes").kind == chat_metrics.COMMAND

    def test_an_agent_in_this_process(self) -> None:
        runtime.registry = _Registry(_Agent("weather"))  # pyright: ignore[reportAttributeAccessIssue]

        destination = chat.destination_of("@weather is it raining?")

        assert destination.kind == chat_metrics.LOCAL
        assert (destination.name, destination.text) == ("weather", "is it raining?")
        assert destination.target is not None

    def test_a_name_nothing_answers_to(self) -> None:
        assert chat.destination_of("@ghost hello").kind == chat_metrics.UNROUTED


class TestRoutingATurn:
    async def test_an_answered_turn_is_recorded_with_its_first_reply(self) -> None:
        runtime.registry = _Registry(_Agent("weather"))  # pyright: ignore[reportAttributeAccessIssue]
        turns, firsts = _turns(chat_metrics.LOCAL), _first_replies(chat_metrics.LOCAL)
        said: list[str] = []

        async def _reply(text: str) -> None:
            said.append(text)

        await chat.route_chat("@weather is it raining?", _reply)

        assert said == ["hello there"]
        assert _turns(chat_metrics.LOCAL) == turns + 1
        assert _first_replies(chat_metrics.LOCAL) == firsts + 1

    async def test_a_turn_to_nobody_is_recorded_as_unrouted(self) -> None:
        before = _turns(chat_metrics.UNROUTED)

        await chat.route_chat("@ghost hello", _replies)

        assert _turns(chat_metrics.UNROUTED) == before + 1

    async def test_a_stopped_turn_is_not_recorded(self) -> None:
        # How long a turn ran before someone stopped it says nothing about
        # how long answers take.
        never = asyncio.Event()
        runtime.registry = _Registry(_Agent("slow", answer=never))  # pyright: ignore[reportAttributeAccessIssue]
        before = _turns(chat_metrics.LOCAL)

        turn = asyncio.create_task(chat.route_chat("@slow think hard", _replies))
        await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert _turns(chat_metrics.LOCAL) == before
