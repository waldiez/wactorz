"""A streamed chat turn over `/ws`: chunks, the end of the stream, and stopping it.

A streamed reply is shown chunk by chunk but stored once, whole, when the stream
ends — one chat_log row per turn rather than one per chunk — and it is stored
even if telling the browser the stream ended fails. Stopping a turn still ends
its stream, so the composer comes back, and says it stopped; an error in the
turn is shown as a reply rather than silently dropped.

`tests/test_ws_handler.py` covers connecting, attribution and commands.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import chat, runtime, ws


class _Registry:
    def all_actors(self) -> list[Any]:
        return []

    def find_by_name(self, name: str) -> Any:
        return SimpleNamespace(name=name) if name == "main" else None


class _Db:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = False

    def write_chat_log(self, **kwargs: Any) -> None:
        if self.fail:
            raise OSError("disk full")
        self.rows.append(kwargs)


@pytest.fixture(name="db")
def db_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Db]:
    recorder = _Db()
    monkeypatch.setattr(runtime, "db", recorder)
    monkeypatch.setattr(runtime, "registry", _Registry())
    monkeypatch.setattr(runtime, "mqtt_connected", True)
    monkeypatch.setattr(runtime, "ws_clients", set())
    yield recorder


@pytest.fixture(name="client")
async def client_fixture(db: _Db) -> AsyncIterator[TestClient[Any, Any]]:
    app = web.Application()
    app.router.add_get("/ws", ws.ws_handler)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def _frames_until(socket: Any, last_type: str) -> list[dict[str, Any]]:
    """Frames after the opening two, up to and including the first of `last_type`."""
    frames: list[dict[str, Any]] = []
    while True:
        msg = await socket.receive(timeout=5)
        frame = json.loads(msg.data)
        if frame["type"] in ("full_snapshot", "mqtt_status"):
            continue
        frames.append(frame)
        if frame["type"] == last_type:
            return frames


async def _wait_for_rows(db: _Db, count: int) -> None:
    for _ in range(200):
        if len(db.rows) >= count:
            return
        await asyncio.sleep(0.01)


class TestStreaming:
    async def test_chunks_are_shown_and_the_turn_is_stored_once(
        self, client: TestClient[Any, Any], db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _route(
            text: str, reply: Any, stream_fn: Any, stream_end_fn: Any, attachments: Any
        ) -> None:
            await stream_fn("Hel")
            await stream_fn("")
            await stream_fn("lo")
            await stream_end_fn()

        monkeypatch.setattr(chat, "route_chat", _route)

        async with client.ws_connect("/ws") as socket:
            await socket.send_str(json.dumps({"type": "chat", "content": "@main hi"}))
            frames = await _frames_until(socket, "stream_end")
            await _wait_for_rows(db, 2)

        assert [f["type"] for f in frames] == [
            "stream_chunk",
            "stream_chunk",
            "stream_chunk",
            "stream_end",
        ]
        assert [(r["role"], r["content"]) for r in db.rows] == [
            ("user", "@main hi"),
            ("assistant", "Hello"),
        ]

    async def test_a_stopped_turn_ends_its_stream_and_says_so(
        self, client: TestClient[Any, Any], db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _route(
            text: str, reply: Any, stream_fn: Any, stream_end_fn: Any, attachments: Any
        ) -> None:
            await stream_fn("partial")
            raise asyncio.CancelledError

        monkeypatch.setattr(chat, "route_chat", _route)

        async with client.ws_connect("/ws") as socket:
            await socket.send_str(json.dumps({"type": "chat", "content": "@main go"}))
            frames = await _frames_until(socket, "chat")

        assert [f["type"] for f in frames] == ["stream_chunk", "stream_end", "chat"]
        assert frames[-1]["content"] == "⏹ Stopped."

    async def test_an_error_in_the_turn_is_shown_as_a_reply(
        self, client: TestClient[Any, Any], db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _route(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(chat, "route_chat", _route)

        async with client.ws_connect("/ws") as socket:
            await socket.send_str(json.dumps({"type": "chat", "content": "@main go"}))
            frames = await _frames_until(socket, "stream_end")

        assert frames[0] == {**frames[0], "type": "chat", "content": "[error] model unavailable"}

    async def test_without_a_registry_chat_is_refused(
        self, client: TestClient[Any, Any], db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", None)

        async with client.ws_connect("/ws") as socket:
            await socket.send_str(json.dumps({"type": "chat", "content": "hello"}))
            frames = await _frames_until(socket, "chat")

        assert frames[0]["content"] == "[system] Chat unavailable — no actor registry."

    async def test_a_failing_chat_log_never_reaches_the_socket(
        self, client: TestClient[Any, Any], db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db.fail = True

        async def _route(text: str, reply: Any, **_kwargs: Any) -> None:
            await reply("answer")

        monkeypatch.setattr(chat, "route_chat", _route)

        async with client.ws_connect("/ws") as socket:
            await socket.send_str(json.dumps({"type": "chat", "content": "@main hi"}))
            frames = await _frames_until(socket, "chat")

        assert frames[0]["content"] == "answer"


class _Socket:
    """Stands in for the WebSocketResponse, failing sends after `sent` frames."""

    def __init__(self, messages: list[Any], fail_after: int) -> None:
        self._messages = messages
        self._fail_after = fail_after
        self.sent: list[dict[str, Any]] = []

    async def prepare(self, _request: Any) -> None:
        return None

    async def send_str(self, data: str) -> None:
        if len(self.sent) >= self._fail_after:
            raise ConnectionResetError("browser went away")
        self.sent.append(json.loads(data))

    def __aiter__(self) -> "_Socket":
        return self

    async def __anext__(self) -> Any:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self) -> None:
        return None


class TestAFailingSocket:
    async def test_a_stream_that_cannot_be_ended_is_still_stored(
        self, db: _Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        done = asyncio.Event()

        async def _route(
            text: str, reply: Any, stream_fn: Any, stream_end_fn: Any, attachments: Any
        ) -> None:
            await stream_fn("kept")
            await stream_end_fn()
            await reply("unsent")
            done.set()

        socket = _Socket(
            [
                SimpleNamespace(
                    type=WSMsgType.TEXT, data=json.dumps({"type": "chat", "content": "@main hi"})
                ),
                SimpleNamespace(type=WSMsgType.TEXT, data="{not json"),
                SimpleNamespace(type=WSMsgType.CLOSE, data=None),
            ],
            fail_after=3,
        )
        monkeypatch.setattr(ws.web, "WebSocketResponse", lambda heartbeat: socket)
        monkeypatch.setattr(ws.origins, "refuse", lambda request, strict_origin: None)
        monkeypatch.setattr(chat, "route_chat", _route)

        await ws.ws_handler(SimpleNamespace())  # pyright: ignore[reportArgumentType]
        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        waiter = asyncio.ensure_future(done.wait())
        finished, _ = await asyncio.wait({waiter}, timeout=5)
        assert waiter in finished

        assert [(r["role"], r["content"]) for r in db.rows] == [
            ("user", "@main hi"),
            ("assistant", "kept"),
        ]
        assert runtime.ws_clients == set()
