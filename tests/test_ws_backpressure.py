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
from aiohttp import ClientSession, ClientWSTimeout, web

from tests.waiting import PATIENCE_S, until
from wactorz.web import runtime, ws

#: Enough of them that some leave at the moment that matters: not only while a
#: frame is being written, but while the socket is still compressing it.
CLIENTS_THAT_LEAVE = 12

#: Large enough that the socket compresses it in a task of its own.
LARGE_FRAME = "x" * 300_000


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
            # The slow client is released only after this: waiting on it would
            # be waiting for ever, so the limit can be a generous one.
            await asyncio.wait_for(ws.broadcast({"type": "patch"}), timeout=PATIENCE_S)
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


class TestClosingWhileAFrameIsBeingWritten:
    """A browser tab that is closed while the server is writing to it.

    The socket's write of a large frame is a task of its own that a cancellation
    does not reach. Cancelled around it, the writer left that task to fail
    against the closing socket with nobody to take the error, and the event loop
    logged it as an error with a traceback, once for every tab closed at a busy
    moment.
    """

    async def test_the_write_is_left_to_end_and_its_failure_is_not_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("DEBUG")
        fails = asyncio.Event()
        ended: list[str] = []

        class _ClosingSocket:
            async def send_str(self, _payload: str) -> None:
                try:
                    await fails.wait()
                except asyncio.CancelledError:
                    ended.append("cancelled")
                    raise
                ended.append("failed")
                raise ConnectionResetError("Cannot write to closing transport")

            async def close(self) -> None:
                return None

        channel = ws.Channel(_ClosingSocket())  # type: ignore[arg-type]
        channel.send("a frame")
        await until(lambda: channel._sending, "the writer to be writing the frame")

        closing = asyncio.create_task(channel.close())
        await until(lambda: channel._closing, "the close to have begun")
        # The transport gives up on the write once the socket has closed.
        fails.set()
        await asyncio.wait_for(closing, PATIENCE_S)

        assert ended == ["failed"], "the write was cancelled, not left to end"
        assert channel._writer.done()
        assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]

    async def test_a_client_that_never_takes_the_frame_does_not_hold_the_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws, "CLOSE_WAIT_S", 0.05)
        channel = ws.Channel(_SlowSocket())  # type: ignore[arg-type]
        channel.send("a frame")
        await until(lambda: channel._sending, "the writer to be writing the frame")

        await asyncio.wait_for(channel.close(), PATIENCE_S)

        assert channel._writer.done()

    async def test_a_channel_with_nothing_being_written_closes_at_once(self) -> None:
        channel = ws.Channel(_FastSocket())  # type: ignore[arg-type]

        await asyncio.wait_for(channel.close(), 1.0)

        assert channel._writer.done()


class TestWithARealSocket:
    """The same, over a real connection: the write that is orphaned is aiohttp's own."""

    async def test_clients_leaving_mid_frame_hand_the_loop_no_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Each of these clients has stopped reading, so its last frame waits for
        # it until the channel gives up: shortened, or the test is that wait.
        monkeypatch.setattr(ws, "CLOSE_WAIT_S", 0.25)
        handed: list[str] = []
        left_mid_frame: list[bool] = []
        loop = asyncio.get_running_loop()
        before = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: handed.append(str(context)))

        async def serve(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse(compress=True)
            await socket.prepare(request)
            channel = ws.Channel(socket)
            leaving = asyncio.Event()

            async def keep_writing() -> None:
                # Always one frame waiting and never a backlog, which the
                # channel would answer by rebuilding a whole snapshot.
                while not leaving.is_set():
                    if channel._queue.empty():
                        channel.send(LARGE_FRAME)
                    await asyncio.sleep(0)

            writing = asyncio.create_task(keep_writing())
            try:
                async for _ in socket:
                    pass
            finally:
                leaving.set()
                await writing
                left_mid_frame.append(channel._sending)
                await channel.close()
            return socket

        app = web.Application()
        app.router.add_get("/ws", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            async with ClientSession() as session:
                for _ in range(CLIENTS_THAT_LEAVE):
                    client = await session.ws_connect(
                        f"http://127.0.0.1:{port}/ws",
                        compress=15,
                        # It leaves without waiting to be told goodbye, as a
                        # closed tab does. The class is declared with attrs'
                        # older syntax, whose keywords the checker does not see.
                        timeout=ClientWSTimeout(ws_close=0.05),  # pyright: ignore[reportCallIssue]
                    )
                    await client.receive()
                    await client.close()
        finally:
            await runner.cleanup()
            loop.set_exception_handler(before)

        # Clients did leave with a frame on its way to them: the run did not
        # pass by every one of them leaving between two frames.
        assert sum(left_mid_frame) >= 1
        assert handed == []
