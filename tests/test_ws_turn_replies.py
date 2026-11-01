"""Each chat turn on a socket answers through its own replies.

The turns a socket carries run at the same time. A turn that ends while another
is still streaming must close its own reply, not the other's, and must not file
the other's text. `tests/test_ws_streaming.py` drives the same thing over a real
socket.
"""

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from aiohttp import web

from wactorz.web import chat, runtime
from wactorz.web.ws import TurnReplies

MAIN = chat.TurnOwner("main", "11111111-1111-4111-8111-111111111111")
CATALOG = chat.TurnOwner("catalog", "22222222-2222-4222-8222-222222222222")


class _Socket:
    """Stands in for the browser's socket: records every frame sent, until it goes."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.gone = False

    async def send_str(self, data: str) -> None:
        if self.gone:
            raise ConnectionResetError
        self.frames.append(json.loads(data))


class _Db:
    """Stands in for the chat log: records each row stored."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def write_chat_log(self, **row: Any) -> None:
        self.rows.append((row["role"], row["content"], row["agent_name"], row["agent_id"]))


@pytest.fixture(name="db")
def db_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Db]:
    recorder = _Db()
    monkeypatch.setattr(runtime, "db", recorder)
    yield recorder


def _replies(socket: _Socket, owner: chat.TurnOwner) -> TurnReplies:
    return TurnReplies(cast(web.WebSocketResponse, socket), owner)


async def test_a_turn_that_ends_does_not_end_another(db: _Db) -> None:
    socket = _Socket()
    deploy, catalog = _replies(socket, MAIN), _replies(socket, CATALOG)

    await deploy.chunk("[deploy] Deploying...")
    await catalog.end()  # the earlier turn, finishing late
    await deploy.chunk("[OK] live")
    await deploy.end()

    assert db.rows == [("assistant", "[deploy] Deploying...[OK] live", "main", MAIN.agent_id)]
    ends = [frame["from"] for frame in socket.frames if frame["type"] == "stream_end"]
    assert ends == ["catalog", "main"]


async def test_each_turn_is_attributed_to_its_own_agent(db: _Db) -> None:
    socket = _Socket()
    main, catalog = _replies(socket, MAIN), _replies(socket, CATALOG)

    await main.chunk("thinking")
    await catalog.reply("'timeseries-collector' spawned")
    await main.end()

    assert [(f["type"], f["from"], f["agent_id"]) for f in socket.frames] == [
        ("stream_chunk", "main", MAIN.agent_id),
        ("chat", "catalog", CATALOG.agent_id),
        ("stream_end", "main", MAIN.agent_id),
    ]
    assert db.rows == [
        ("assistant", "'timeseries-collector' spawned", "catalog", CATALOG.agent_id),
        ("assistant", "thinking", "main", MAIN.agent_id),
    ]


async def test_two_turns_of_one_agent_store_a_reply_each(db: _Db) -> None:
    socket = _Socket()
    first, second = _replies(socket, MAIN), _replies(socket, MAIN)

    await first.chunk("one")
    await second.chunk("two")
    await first.end()
    await second.end()

    assert [row[1] for row in db.rows] == ["one", "two"]


async def test_a_streamed_reply_is_stored_once_even_if_the_socket_has_gone(db: _Db) -> None:
    socket = _Socket()
    turn = _replies(socket, MAIN)
    await turn.chunk("said before it went")
    socket.gone = True

    await turn.end()
    await turn.end()

    assert db.rows == [("assistant", "said before it went", "main", MAIN.agent_id)]


async def test_an_offer_carries_the_addresses_and_what_a_pick_sends_again(db: _Db) -> None:
    socket = _Socket()

    await _replies(socket, MAIN).offer("Which one?", ("tally@pi", "tally@main"), retry_of="m1")

    assert socket.frames[0]["choices"] == ["tally@pi", "tally@main"]
    assert socket.frames[0]["retry_of"] == "m1"
    assert [row[1] for row in db.rows] == ["Which one?"]
