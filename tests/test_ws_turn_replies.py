"""Each chat turn on a socket answers through its own replies.

The turns a socket carries run at the same time. A turn that ends while another
is still streaming -- one that waited for its answer to be spoken aloud, say --
must close its own reply, not the other's, and must not file the other's text.
"""

import json
from typing import Any, cast

from aiohttp import web

from wactorz.web.ws import TurnReplies


class _Socket:
    """Stands in for the browser's socket: records every frame sent."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_str(self, data: str) -> None:
        self.frames.append(json.loads(data))


class _Log:
    """Stands in for the chat log: records each row stored."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def __call__(self, role: str, content: str, agent: str) -> None:
        self.rows.append((role, content, agent))


def _turns() -> tuple[_Socket, _Log, TurnReplies, TurnReplies]:
    socket, log = _Socket(), _Log()
    ws = cast(web.WebSocketResponse, socket)
    return socket, log, TurnReplies(ws, "main", log), TurnReplies(ws, "catalog", log)


async def test_a_turn_that_ends_does_not_end_another() -> None:
    socket, log, deploy, catalog = _turns()

    await deploy.chunk("[deploy] Deploying...")
    await catalog.end()  # the earlier turn, finishing late
    await deploy.chunk("[OK] live")
    await deploy.end()

    assert log.rows == [("assistant", "[deploy] Deploying...[OK] live", "main")]
    ends = [frame["from"] for frame in socket.frames if frame["type"] == "stream_end"]
    assert ends == ["catalog", "main"]


async def test_each_turn_is_attributed_to_its_own_agent() -> None:
    socket, log, main, catalog = _turns()

    await main.chunk("thinking")
    await catalog.reply("'timeseries-collector' spawned")
    await main.end()

    assert [(f["type"], f["from"]) for f in socket.frames] == [
        ("stream_chunk", "main"),
        ("chat", "catalog"),
        ("stream_end", "main"),
    ]
    assert log.rows == [
        ("assistant", "'timeseries-collector' spawned", "catalog"),
        ("assistant", "thinking", "main"),
    ]


async def test_a_streamed_reply_is_stored_once_even_if_the_socket_has_gone() -> None:
    class _Gone(_Socket):
        async def send_str(self, data: str) -> None:
            raise ConnectionResetError

    log = _Log()
    turn = TurnReplies(cast(web.WebSocketResponse, _Gone()), "main", log)
    turn._streamed.append("said before it went")  # pyright: ignore[reportPrivateUsage]

    await turn.end()
    await turn.end()

    assert log.rows == [("assistant", "said before it went", "main")]
