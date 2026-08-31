"""An agent that is not running must not answer chat.

Stopping cancels the message loop that drains the mailbox, but chat never uses
the mailbox -- it calls ``process_user_input``, ``chat`` or ``handle_message``
on the agent directly. The object is still there and its methods still work, so
the state has to be checked here or the one way a user actually talks to an
agent goes straight past it.
"""

from collections.abc import Iterator

import pytest

from wactorz.core.actor import Actor, ActorState, Message
from wactorz.web import chat, runtime


class _Talker(Actor):
    """An agent that answers chat, and records having been asked."""

    def __init__(self, name: str = "weather-agent") -> None:
        super().__init__(name=name)
        self.asked: list[str] = []

    async def process_user_input(self, text: str) -> str:
        self.asked.append(text)
        return "sunny"

    async def handle_message(self, message: Message) -> None:
        return None


class _Registry:
    def __init__(self, actor: Actor) -> None:
        self._actor = actor

    def get(self, actor_id: str) -> Actor | None:
        return self._actor if self._actor.actor_id == actor_id else None

    def find_by_name(self, name: str) -> Actor | None:
        return self._actor if self._actor.name == name else None


@pytest.fixture(name="agent")
def agent_fixture() -> _Talker:
    """A registered agent, with the registry restored afterwards."""
    return _Talker()


@pytest.fixture(autouse=True)
def _restore_runtime() -> Iterator[None]:
    registry = runtime.registry
    yield
    runtime.registry = registry


async def _say(text: str) -> tuple[list[str], int]:
    """Route one chat turn, returning the replies and the stream-end count."""
    replies: list[str] = []
    ends = {"n": 0}

    async def _reply(message: str) -> None:
        replies.append(message)

    async def _end() -> None:
        ends["n"] += 1

    await chat.route_chat(text, _reply, stream_end_fn=_end)
    return replies, ends["n"]


class TestAStoppedAgent:
    async def test_it_does_not_answer(self, agent: _Talker) -> None:
        runtime.registry = _Registry(agent)
        agent.state = ActorState.STOPPED

        replies, _ends = await _say("@weather-agent what is the weather")

        # stop() cancels the message loop, but chat never used it — the agent
        # object is still there and its methods still work.
        assert agent.asked == []
        assert "stopped" in replies[0].lower()

    async def test_the_turn_is_ended_so_the_input_re_enables(self, agent: _Talker) -> None:
        runtime.registry = _Registry(agent)
        agent.state = ActorState.STOPPED

        _replies, ends = await _say("@weather-agent hello")

        # Without this the browser waits for a stream that never ends.
        assert ends == 1

    async def test_it_answers_again_once_started(self, agent: _Talker) -> None:
        runtime.registry = _Registry(agent)
        agent.state = ActorState.STOPPED
        await _say("@weather-agent first")

        agent.state = ActorState.RUNNING
        replies, _ends = await _say("@weather-agent second")

        assert agent.asked == ["second"]
        assert replies == ["sunny"]


class TestAFailedAgent:
    async def test_it_does_not_answer(self, agent: _Talker) -> None:
        runtime.registry = _Registry(agent)
        agent.state = ActorState.FAILED

        replies, _ends = await _say("@weather-agent hello")

        # The supervisor is about to restart it; answering from a failed agent
        # would be answering from something already being replaced.
        assert agent.asked == []
        assert "failed" in replies[0].lower()


class TestARunningAgent:
    async def test_it_still_answers(self, agent: _Talker) -> None:
        runtime.registry = _Registry(agent)
        agent.state = ActorState.RUNNING

        replies, _ends = await _say("@weather-agent what is the weather")

        assert agent.asked == ["what is the weather"]
        assert replies == ["sunny"]
