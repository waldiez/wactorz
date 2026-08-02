"""One slow browser must not stall the broker.

The regression this pins: ``broadcast`` awaited ``send_str`` for every client in
turn, from inside the MQTT message loop. A client on a slow link therefore
delayed *ingest* — for every other client, and for the broker read itself.

Each client now owns a queue and a writer task, so falling behind is a private
problem. The second property matters as much as the first: a client that falls
too far behind is resynchronised rather than quietly fed a truncated stream,
because a browser applying patches to a base it never received is wrong without
knowing it.
"""

import asyncio
import json

import pytest

from wactorz.web import runtime, ws


class _SlowSocket:
    """A client that never finishes sending."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.release = asyncio.Event()

    async def send_str(self, payload: str) -> None:
        await self.release.wait()
        self.sent.append(payload)


class _FastSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)


@pytest.fixture(autouse=True)
def _clean_clients():
    original = set(runtime.ws_clients)
    runtime.ws_clients.clear()
    yield
    runtime.ws_clients.clear()
    runtime.ws_clients.update(original)


class TestASlowClientIsIsolated:
    async def test_broadcast_does_not_wait_for_a_slow_client(self) -> None:
        slow = _SlowSocket()
        channel = ws.Channel(slow)  # type: ignore[arg-type]
        runtime.ws_clients.add(channel)
        try:
            await asyncio.wait_for(ws.broadcast({"type": "patch"}), timeout=0.5)
        finally:
            slow.release.set()
            await channel.close()

    async def test_a_fast_client_is_served_while_a_slow_one_blocks(self) -> None:
        slow, fast = _SlowSocket(), _FastSocket()
        slow_ch = ws.Channel(slow)  # type: ignore[arg-type]
        fast_ch = ws.Channel(fast)  # type: ignore[arg-type]
        runtime.ws_clients.update({slow_ch, fast_ch})
        try:
            await ws.broadcast({"type": "patch", "n": 1})
            await asyncio.sleep(0)  # let the writers run
            await asyncio.sleep(0)
            assert fast.sent, "the fast client waited on the slow one"
            assert not slow.sent
        finally:
            slow.release.set()
            await slow_ch.close()
            await fast_ch.close()


class TestFallingBehindResyncs:
    async def test_overflow_replaces_the_backlog_with_a_full_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws.events, "snapshot", lambda *a, **k: {"resynced": True})
        slow = _SlowSocket()
        channel = ws.Channel(slow)  # type: ignore[arg-type]
        try:
            for i in range(ws._CLIENT_QUEUE_DEPTH + 50):
                channel.send(json.dumps({"type": "patch", "n": i}))
            assert channel.dropped > 0, "the queue never overflowed"

            slow.release.set()
            for _ in range(20):
                await asyncio.sleep(0)

            assert slow.sent, "nothing was delivered after the client caught up"
            frames = [json.loads(f) for f in slow.sent]
            assert any(f.get("type") == "full_snapshot" for f in frames), (
                "a client that fell behind was fed a truncated stream instead of a resync"
            )
        finally:
            await channel.close()

    async def test_no_overflow_delivers_every_frame_in_order(self) -> None:
        fast = _FastSocket()
        channel = ws.Channel(fast)  # type: ignore[arg-type]
        try:
            for i in range(10):
                channel.send(json.dumps({"n": i}))
            for _ in range(30):
                await asyncio.sleep(0)
            assert [json.loads(f)["n"] for f in fast.sent] == list(range(10))
            assert channel.dropped == 0
        finally:
            await channel.close()


class TestDeadClientsAreRemoved:
    async def test_a_failing_socket_drops_out_of_the_broadcast_set(self) -> None:
        class _Broken:
            async def send_str(self, _payload: str) -> None:
                raise ConnectionResetError("gone")

        channel = ws.Channel(_Broken())  # type: ignore[arg-type]
        runtime.ws_clients.add(channel)
        channel.send(json.dumps({"type": "patch"}))
        for _ in range(20):
            await asyncio.sleep(0)
        assert channel not in runtime.ws_clients


class TestTheWriterFailsSafely:
    """A dead writer is a client that silently stops updating, so the writer has
    to survive anything recoverable — and its death must not surface somewhere
    unrelated."""

    async def test_a_failing_resync_does_not_kill_the_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Building a resync reads the database, so it can fail."""

        def boom(*_a: object, **_k: object) -> dict:
            raise RuntimeError("database gone")

        monkeypatch.setattr(ws.events, "snapshot", boom)
        fast = _FastSocket()
        channel = ws.Channel(fast)  # type: ignore[arg-type]
        try:
            for i in range(ws._CLIENT_QUEUE_DEPTH + 50):
                channel.send(json.dumps({"n": i}))
            for _ in range(40):
                await asyncio.sleep(0)

            assert not channel._writer.done(), "the writer died on a failed resync"
            # And it still serves the next frame.
            channel.send(json.dumps({"after": True}))
            for _ in range(20):
                await asyncio.sleep(0)
            assert any("after" in f for f in fast.sent)
        finally:
            await channel.close()

    async def test_close_does_not_re_raise_a_dead_writer(self) -> None:
        """``close`` runs from the handler's ``finally``.

        Re-raising there would replace whatever actually brought the connection
        down with the writer's own error. The writer is driven into a failed
        state directly: its own code no longer has a path that dies
        exceptionally, and this pins the contract rather than today's reachable
        set of failures.
        """

        async def explode() -> None:
            raise RuntimeError("writer died")

        channel = ws.Channel(_FastSocket())  # type: ignore[arg-type]
        await channel.close()  # stop the real writer first
        channel._writer = asyncio.create_task(explode())
        for _ in range(10):
            await asyncio.sleep(0)
        assert channel._writer.done()

        await channel.close()  # must not raise

    async def test_a_failed_send_closes_the_socket(self) -> None:
        """Otherwise the browser keeps a working command channel with no updates."""
        closed = asyncio.Event()

        class _Broken:
            async def send_str(self, _payload: str) -> None:
                raise ConnectionResetError("gone")

            async def close(self) -> None:
                closed.set()

        channel = ws.Channel(_Broken())  # type: ignore[arg-type]
        runtime.ws_clients.add(channel)
        channel.send(json.dumps({"type": "patch"}))
        for _ in range(20):
            await asyncio.sleep(0)
        assert closed.is_set(), "the socket was left open after the writer gave up"
        assert channel not in runtime.ws_clients
