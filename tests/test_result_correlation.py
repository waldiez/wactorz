"""A RESULT carrying a correlation id settles the future waiting on it.

Request/reply is a convention, not a framework feature: a sender tags a TASK
with `_task_id` and blocks on a future keyed by it, and the recipient echoes
that id back.

The matching half runs once, in `Actor._dispatch`, ahead of any handler. That
is what makes the ability to receive a reply a property of being an actor
rather than of which agent happens to be receiving.
"""

import asyncio
from pathlib import Path

import pytest

from wactorz.core.actor import Actor, Message, MessageType


class _Agent(Actor):
    """Records what reached `handle_message`, so interception is observable."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(name="worker", persistence_dir=str(tmp_path))
        self.seen: list[Message] = []

    async def handle_message(self, message: Message) -> None:
        self.seen.append(message)


@pytest.fixture(name="agent")
def agent_fixture(tmp_path: Path) -> _Agent:
    return _Agent(tmp_path)


def _result(payload: object, sender: str = "other") -> Message:
    return Message(type=MessageType.RESULT, sender_id=sender, payload=payload)


class TestCorrelatedResults:
    async def test_a_matching_result_settles_the_future(self, agent: _Agent) -> None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        agent._result_futures["t-1"] = future

        await agent._dispatch(_result({"_task_id": "t-1", "value": 7}))

        assert future.result() == {"_task_id": "t-1", "value": 7}

    async def test_a_settled_result_does_not_also_reach_the_handler(self, agent: _Agent) -> None:
        agent._result_futures["t-1"] = asyncio.get_running_loop().create_future()

        await agent._dispatch(_result({"_task_id": "t-1"}))

        # The caller is already awaiting it; delivering it again would run the
        # agent's own RESULT handling for a reply that was never its business.
        assert agent.seen == []

    async def test_the_legacy_task_key_still_correlates(self, agent: _Agent) -> None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        agent._result_futures["t-2"] = future

        await agent._dispatch(_result({"task": "t-2", "value": "ok"}))

        assert future.done()

    async def test_an_already_settled_future_is_left_alone(self, agent: _Agent) -> None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.set_result({"first": True})
        agent._result_futures["t-3"] = future

        # A duplicate reply must not raise InvalidStateError on the loop.
        await agent._dispatch(_result({"_task_id": "t-3", "second": True}))

        assert future.result() == {"first": True}


class TestUncorrelatedResults:
    async def test_an_unknown_id_reaches_the_handler(self, agent: _Agent) -> None:
        await agent._dispatch(_result({"_task_id": "never-sent"}))

        assert len(agent.seen) == 1

    async def test_a_result_with_no_id_reaches_the_handler(self, agent: _Agent) -> None:
        await agent._dispatch(_result({"reply": "hello"}))

        assert len(agent.seen) == 1

    async def test_a_non_dict_result_reaches_the_handler(self, agent: _Agent) -> None:
        # Agents may answer with a bare string; that cannot carry an id, so it
        # must not be mistaken for a correlated reply.
        await agent._dispatch(_result("just text"))

        assert len(agent.seen) == 1

    async def test_a_task_is_never_intercepted(self, agent: _Agent) -> None:
        # A TASK may legitimately carry `_task_id` — it is the request, not the
        # reply, and intercepting it would stop the agent doing the work.
        agent._result_futures["t-4"] = asyncio.get_running_loop().create_future()

        await agent._dispatch(
            Message(type=MessageType.TASK, sender_id="other", payload={"_task_id": "t-4"})
        )

        assert len(agent.seen) == 1


class TestEveryActorHasIt:
    async def test_the_map_exists_without_anyone_creating_it(self, agent: _Agent) -> None:
        # Senders can key a future without first checking the map exists.
        assert agent._result_futures == {}
