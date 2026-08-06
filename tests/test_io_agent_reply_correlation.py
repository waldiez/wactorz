"""A reply must clear the request that produced it, not whichever came first.

The gateway sends a TASK per chat message and holds the requester against it
until the answer arrives. With two requests in flight, the order the replies
arrive in says nothing about which request each answers — so the match has to
be on the correlation id agents echo back on the RESULT (see
`LLMAgent._reply_to_task`), never on position.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.io_agent import IOAgent
from wactorz.core.actor import Message, MessageType


class _Recorder(IOAgent):
    """An IOAgent whose replies are captured instead of published."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(persistence_dir=str(tmp_path))
        self.replies: list[tuple[str, str | None]] = []

    async def _reply(self, content: str, to: str | None = None) -> None:
        self.replies.append((content, to))

    async def _mqtt_publish(self, topic: str, payload: Any, **_kw: Any) -> None:
        """Never touch a broker from a test."""


@pytest.fixture(name="gateway")
def gateway_fixture(tmp_path: Path) -> _Recorder:
    return _Recorder(tmp_path)


def _result(task_id: str | None, text: str) -> Message:
    payload: dict[str, Any] = {"reply": text}
    if task_id is not None:
        payload["_task_id"] = task_id
    return Message(type=MessageType.RESULT, sender_id="weather", payload=payload)


class TestCorrelation:
    async def test_a_reply_clears_its_own_request(self, gateway: _Recorder) -> None:
        gateway._pending_replies = {"a": ("alice", 1.0), "b": ("bob", 2.0)}

        await gateway.handle_message(_result("b", "sunny"))

        # "a" is still waiting; only b's entry went.
        assert list(gateway._pending_replies) == ["a"]

    async def test_the_reply_is_addressed_to_the_requester(self, gateway: _Recorder) -> None:
        gateway._pending_replies = {"a": ("alice", 1.0), "b": ("bob", 2.0)}

        await gateway.handle_message(_result("b", "sunny"))

        assert gateway.replies == [("sunny", "bob")]

    async def test_out_of_order_replies_each_clear_their_own(self, gateway: _Recorder) -> None:
        gateway._pending_replies = {"a": ("alice", 1.0), "b": ("bob", 2.0)}

        await gateway.handle_message(_result("b", "for bob"))
        await gateway.handle_message(_result("a", "for alice"))

        assert gateway._pending_replies == {}
        assert gateway.replies == [("for bob", "bob"), ("for alice", "alice")]

    async def test_an_uncorrelated_reply_still_reaches_the_user(self, gateway: _Recorder) -> None:
        # An older agent may answer without echoing the id. The reply is worth
        # more than the attribution, so it goes out unaddressed rather than
        # being dropped — but it must not clear an unrelated request.
        gateway._pending_replies = {"a": ("alice", 1.0)}

        await gateway.handle_message(_result(None, "no id"))

        assert gateway.replies == [("no id", None)]
        assert list(gateway._pending_replies) == ["a"]

    async def test_an_unknown_id_does_not_clear_anything(self, gateway: _Recorder) -> None:
        gateway._pending_replies = {"a": ("alice", 1.0)}

        await gateway.handle_message(_result("never-sent", "stray"))

        assert list(gateway._pending_replies) == ["a"]


class TestExpiry:
    """The timestamp was recorded from the start but nothing ever read it."""

    async def test_a_request_that_is_never_answered_is_forgotten(self, gateway: _Recorder) -> None:
        stale = 1.0  # epoch-old, far beyond any TTL
        gateway._pending_replies = {"old": ("alice", stale)}

        gateway._expire_pending()

        assert gateway._pending_replies == {}

    async def test_a_recent_request_is_kept(self, gateway: _Recorder) -> None:
        gateway._pending_replies = {"fresh": ("alice", time.time())}

        gateway._expire_pending()

        # Slow is not the same as lost — a long LLM turn must still get its
        # reply attributed when it lands.
        assert list(gateway._pending_replies) == ["fresh"]
