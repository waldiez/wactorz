"""Two people talking to the same agent must not get each other's replies.

Message-passing agents reply to `msg.reply_to or msg.sender_id`, and a chat turn
is not a real actor, so the reply has to be intercepted. That was done by
swapping the agent's `send` for the duration of a turn and restoring it after —
which only works if turns never overlap.

They do overlap. Two browser tabs, or one person messaging an agent that is
still answering someone else, is enough:

  A saves the real send, installs capture_A
  B saves target.send — now capture_A, not the real method — installs capture_B
  A restores the real send, so B's replies stop being captured
  B restores capture_A, leaving the agent replying into A's abandoned queue

The agent is then unable to answer anyone until it is restarted.
"""

import asyncio
from typing import Any

import pytest

from wactorz.core.actor import Actor, Message, MessageType
from wactorz.web import chat, runtime


class _Worker(Actor):
    """An agent that answers a TASK by sending a RESULT to its reply address."""

    def __init__(self, name: str = "worker") -> None:
        super().__init__(name=name)
        self.gate: asyncio.Event | None = None
        #: released once per turn that has reached the gate, so a test can wait
        #: for real overlap rather than hoping a sleep(0) produced it.
        self.arrived = asyncio.Semaphore(0)

    async def handle_message(self, message: Message) -> None:
        # `!=`, not `is not`: a full-suite run loads wactorz.core.actor under
        # two module identities, so the enum members are distinct objects with
        # equal values. Identity here made this agent ignore every TASK and the
        # turn waited out its 150s timeout.
        if message.type != MessageType.TASK:
            return
        self.arrived.release()
        if self.gate is not None:
            await self.gate.wait()
        target = message.reply_to or message.sender_id
        await self.send(target, MessageType.RESULT, {"reply": message.payload["text"]})


class _Registry:
    def __init__(self, actor: Actor) -> None:
        self._actor = actor

    def find_by_name(self, name: str) -> Actor | None:
        return self._actor if self._actor.name == name else None

    def get(self, actor_id: str) -> Actor | None:
        return self._actor if self._actor.actor_id == actor_id else None


@pytest.fixture(autouse=True)
def _restore_runtime() -> Any:
    registry = runtime.registry
    yield
    runtime.registry = registry
    chat._PENDING_REPLIES.clear()


@pytest.fixture(name="agent")
def agent_fixture() -> _Worker:
    actor = _Worker()
    runtime.registry = _Registry(actor)  # type: ignore[assignment]
    return actor


async def _turn(text: str) -> list[str]:
    """Run one chat turn, returning whatever was said back."""
    replies: list[str] = []

    async def _reply(message: str) -> None:
        replies.append(message)

    await chat.route_chat(f"@worker {text}", _reply)
    return replies


async def _both_inside(agent: "_Worker", count: int) -> None:
    """Block until `count` turns are all inside handle_message.

    Without this the turns run one after another and the test proves nothing:
    a single `sleep(0)` does not get either of them as far as the dispatch.
    """
    for _ in range(count):
        await asyncio.wait_for(agent.arrived.acquire(), timeout=2)


class TestOneTurn:
    async def test_a_reply_reaches_the_asker(self, agent: _Worker) -> None:
        assert await _turn("hello") == ["hello"]

    async def test_nothing_is_left_pending_afterwards(self, agent: _Worker) -> None:
        await _turn("hello")

        # A leaked entry is a queue nobody will ever read, held for the life of
        # the process.
        assert chat._PENDING_REPLIES == {}


class TestConcurrentTurns:
    async def test_each_turn_gets_its_own_reply(self, agent: _Worker) -> None:
        agent.gate = asyncio.Event()

        first = asyncio.create_task(_turn("first"))
        second = asyncio.create_task(_turn("second"))
        await _both_inside(agent, 2)
        agent.gate.set()

        # Each turn is answered with its own text. Capturing "any RESULT" meant
        # whichever queue was installed took whichever reply arrived.
        assert await first == ["first"]
        assert await second == ["second"]

    async def test_the_agent_still_works_after_overlapping_turns(self, agent: _Worker) -> None:
        agent.gate = asyncio.Event()
        overlapping = [asyncio.create_task(_turn(f"n{i}")) for i in range(3)]
        await _both_inside(agent, 3)
        agent.gate.set()
        await asyncio.gather(*overlapping)

        agent.gate = None
        # The restore dance left send() pointing at a dead capture, so every
        # later reply went nowhere — the agent was mute until restarted.
        assert await _turn("after") == ["after"]

    async def test_no_correlation_ids_are_leaked(self, agent: _Worker) -> None:
        agent.gate = asyncio.Event()
        turns = [asyncio.create_task(_turn(f"n{i}")) for i in range(3)]
        await _both_inside(agent, 3)
        agent.gate.set()
        await asyncio.gather(*turns)

        assert chat._PENDING_REPLIES == {}


class TestOrdinaryResultsStillFlow:
    async def test_a_result_for_a_real_actor_is_not_captured(self, agent: _Worker) -> None:
        await _turn("install the interceptor")

        agent._registry = None  # so the wrapped send has a distinguishable answer
        delivered = await agent.send("not-a-chat-turn", MessageType.RESULT, {"x": 1})

        # False is what the real Actor.send returns with no registry, so the
        # call reached it — a RESULT addressed to something other than a waiting
        # chat turn must fall through rather than be swallowed by the capture.
        assert delivered is False
        assert chat._PENDING_REPLIES == {}
