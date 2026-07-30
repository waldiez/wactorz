"""Home Assistant WebSocket calls must not wait forever — except where they should.

`connect()` sets `ping_interval`/`ping_timeout`, so a *dead* socket is closed by
the library. What that does not cover is a peer that stays alive and simply never
answers: pings are returned, the connection looks healthy, and the awaiting
coroutine blocks indefinitely.

The distinction that matters is request/response versus subscription. A command
either gets its reply promptly or something is wrong. Waiting hours for the next
event, by contrast, is exactly what a subscription is for — putting a deadline
there would break it, so `receive_event` stays unbounded on purpose.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import asyncio
import json

import pytest

from wactorz.core.integrations.home_assistant import ha_web_socket_client as mod
from wactorz.core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient


class _FakeWS:
    """Replays queued frames, then goes silent without closing."""

    def __init__(self, frames: list[dict] | None = None):
        self._frames = [json.dumps(f) for f in (frames or [])]
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)  # alive, answering pings, saying nothing
        raise AssertionError("unreachable")

    async def close(self) -> None:
        pass


def _client(ws: _FakeWS) -> HAWebSocketClient:
    client = HAWebSocketClient("ws://ha.local/api/websocket", "token")
    client._ws = ws  # pyright: ignore[reportAttributeAccessIssue]
    return client


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "_RESPONSE_TIMEOUT", 0.1)


class TestBoundedCalls:
    @pytest.mark.asyncio
    async def test_call_gives_up_on_a_silent_peer(self):
        client = _client(_FakeWS())
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await client.call("get_states")

    @pytest.mark.asyncio
    async def test_call_still_returns_a_normal_reply(self):
        ws = _FakeWS([{"id": 1, "success": True, "result": [{"entity_id": "light.x"}]}])
        result = await _client(ws).call("get_states")
        assert result == [{"entity_id": "light.x"}]

    @pytest.mark.asyncio
    async def test_call_is_bounded_overall_not_merely_per_frame(self):
        """Unrelated chatter must not keep a call alive indefinitely."""
        noise = [{"id": 999, "type": "event"} for _ in range(500)]
        client = _client(_FakeWS(noise))
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await client.call("get_states")

    @pytest.mark.asyncio
    async def test_authenticate_gives_up_on_a_silent_peer(self):
        client = _client(_FakeWS())
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_succeeds_normally(self):
        ws = _FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])
        await _client(ws)._authenticate()
        assert ws.sent[0]["type"] == "auth"

    @pytest.mark.asyncio
    async def test_subscribe_gives_up_on_a_silent_peer(self):
        client = _client(_FakeWS())
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await client.subscribe_events("state_changed")


class TestUnboundedSubscription:
    @staticmethod
    async def _still_waiting_after_the_command_deadline(coro) -> bool:
        """True if `coro` is still pending well past `_RESPONSE_TIMEOUT`.

        Asserting that a TimeoutError is raised proves nothing here: the test's
        own `wait_for` raises the same type the client would, so a wrongly
        bounded client passes either way.
        """
        task = asyncio.ensure_future(coro)
        await asyncio.sleep(mod._RESPONSE_TIMEOUT * 4)
        pending = not task.done()
        task.cancel()
        return pending

    @pytest.mark.asyncio
    async def test_receive_event_is_not_given_a_deadline(self):
        """Hours between events is normal; a timeout here would be the bug."""
        client = _client(_FakeWS())
        assert await self._still_waiting_after_the_command_deadline(client.receive_event(1))

    @pytest.mark.asyncio
    async def test_receive_event_still_delivers_a_matching_event(self):
        ws = _FakeWS([{"type": "event", "id": 7, "event": {"a": 1}}])
        got = await _client(ws).receive_event(7)
        assert got["event"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_receive_json_is_unbounded_by_default(self):
        client = _client(_FakeWS())
        assert await self._still_waiting_after_the_command_deadline(client.receive_json())
