"""The TUI's view of a server, against the real application.

Only what a turn says is faked. The socket, the routes and the key are real, so
this is what a terminal attached to a server sees.
"""

import ast
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

import wactorz.tui
from tests.test_client import KEY, _Db, db_fixture, server_fixture  # noqa: F401
from tests.waiting import until
from wactorz.client import Client, Cost, UnavailableError
from wactorz.tui.attachments import Staged
from wactorz.tui.context import TUIContext, host_stats
from wactorz.web import chat, runtime


@pytest.fixture(name="context")
async def context_fixture(server: TestServer) -> AsyncIterator[TUIContext]:
    async with Client(str(server.make_url("")), api_key=KEY) as client:
        context = TUIContext(client)
        await context.start()
        yield context


async def test_it_reads_what_the_server_says_about_itself(context: TUIContext) -> None:
    assert context.server.version
    assert context.server.upload_max_bytes > 0
    assert context.server.uploads_per_message > 0


async def test_a_refused_key_ends_the_start(server: TestServer) -> None:
    async with Client(str(server.make_url("")), api_key="wrong") as client:
        with pytest.raises(Exception, match="refused the key"):
            await TUIContext(client).start()


async def test_the_snapshot_is_what_the_socket_said(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = {"agent_id": "a1", "name": "worker", "state": "running"}
    monkeypatch.setitem(runtime.state, "agents", {"a1": agent})
    monkeypatch.setattr(runtime, "mqtt_connected", True)
    logs: list[Any] = []
    following = asyncio.create_task(context.follow(logs.extend))

    await until(lambda: context.state.agents, "the first frames")
    snap = await context.snapshot()
    following.cancel()

    assert [agent["name"] for agent in snap.agents] == ["worker"]
    assert snap.connected
    assert snap.broker_connected


async def test_the_periods_spend_and_limit_come_from_the_server(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cost() -> Cost:
        return Cost(spend_usd=5.0, limit_usd=10.0, period="monthly")

    monkeypatch.setattr(context.client.system, "cost", cost)
    context.state.total_cost_usd = 100.0

    snap = await context.snapshot()

    assert (snap.cost_usd, snap.period_spend_usd, snap.cost_limit_usd) == (100.0, 5.0, 10.0)
    assert snap.cost_period == "monthly"


def _line(text: str) -> dict[str, Any]:
    return {"ts": 1.0, "origin": "wactorz.test", "level": "INFO", "text": text}


async def _follow_with_backlog(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch, backlog: list[dict[str, Any]]
) -> tuple[list[Any], asyncio.Task[None]]:
    async def logs(*, limit: int) -> list[dict[str, Any]]:
        return list(backlog)

    monkeypatch.setattr(context.client.system, "logs", logs)
    seen: list[Any] = []
    following = asyncio.create_task(context.follow(seen.extend))
    await until(lambda: seen, "the backlog")
    return seen, following


async def test_live_log_lines_follow_the_backlog_and_a_line_in_both_shows_once(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, following = await _follow_with_backlog(context, monkeypatch, [_line("before")])

    for channel in list(runtime.ws_clients):
        await channel.ws.send_json(
            {"type": "app_log", "entries": [_line("before"), _line("after")]}
        )
    await until(lambda: len(seen) > 1, "the live line")
    following.cancel()

    assert [entry["text"] for entry in seen] == ["before", "after"]


async def test_what_was_logged_while_the_socket_was_down_is_recovered(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = [_line("before")]
    seen, following = await _follow_with_backlog(context, monkeypatch, backlog)

    backlog.append(_line("while away"))
    for channel in list(runtime.ws_clients):
        await channel.ws.close()
    await until(lambda: len(seen) > 1, "the backlog reread")
    following.cancel()

    assert [entry["text"] for entry in seen] == ["before", "while away"]


async def test_a_backlog_that_cannot_be_read_leaves_the_stream_running(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def logs(*, limit: int) -> list[dict[str, Any]]:
        raise UnavailableError("http://server.test", "no")

    monkeypatch.setattr(context.client.system, "logs", logs)
    seen: list[Any] = []
    following = asyncio.create_task(context.follow(seen.extend))

    await until(lambda: context.state.connected, "the socket")
    for channel in list(runtime.ws_clients):
        await channel.ws.send_json({"type": "app_log", "entries": [_line("live")]})
    await until(lambda: seen, "the live line")
    following.cancel()

    assert [entry["text"] for entry in seen] == ["live"]


async def test_history_comes_back_oldest_first(context: TUIContext, db: _Db) -> None:
    db.rows.extend(
        [{"role": "user", "content": "newest"}, {"role": "assistant", "content": "oldest"}]
    )

    rows = await context.history()

    assert [row["content"] for row in rows] == ["oldest", "newest"]


async def test_a_turn_goes_through_the_server(
    context: TUIContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    got: list[Any] = []

    async def route(_text: str, reply: Any, attachments: Any, **_: Any) -> None:
        got.extend(attachments)
        await reply("seen")

    monkeypatch.setattr(chat, "route_chat", route)
    source = tmp_path / "notes.txt"
    source.write_bytes(b"words")

    stored, failed = await context.store_attachments(
        [Staged(path=source, name="notes.txt", size=5)]
    )
    parts = [part async for part in context.chat_stream("hi", stored)]

    assert failed == []
    assert [part.text for part in parts] == ["seen"]
    assert [file["name"] for file in got] == ["notes.txt"]


async def test_a_file_that_will_not_store_is_named_and_the_rest_go(
    context: TUIContext, tmp_path: Path
) -> None:
    good = tmp_path / "good.txt"
    good.write_bytes(b"words")
    empty = tmp_path / "empty.txt"
    empty.touch()

    stored, failed = await context.store_attachments(
        [Staged(good, "good.txt", 5), Staged(empty, "empty.txt", 0)]
    )

    assert [file.name for file in stored] == ["good.txt"]
    assert "empty.txt" in failed[0]


def test_the_servers_vitals_tolerate_what_is_missing() -> None:
    assert host_stats({"cpu": 1.5}).cpu_pct == 1.5
    assert host_stats({}).mem_total_mb == 0.0


def test_a_mention_is_a_guess_at_who_answers_until_the_server_says() -> None:
    attribution = TUIContext.attribution

    assert attribution(_nobody(), "@catalog list") == "catalog"
    assert attribution(_nobody(), "@") == "main"
    assert attribution(_nobody(), "hello") == "main"


def _nobody() -> TUIContext:
    return TUIContext(Client("http://server.test"))


def test_the_tui_reaches_wactorz_only_through_the_client() -> None:
    # A gap in the API shows as a gap, rather than being reached around.
    folder = Path(wactorz.tui.__file__).parent
    allowed = {"..client", "..._version", ".._version"}

    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                # Within the TUI is fine: one dot, or two from a pane to its package.
                below = path.parent != folder
                if node.level == 1 or (below and node.level == 2):
                    continue
                name = "." * node.level + (node.module or "")
                assert name in allowed, f"{path.name}: {name}"
            if isinstance(node, ast.ImportFrom) and not node.level:
                assert not (node.module or "").startswith("wactorz"), path.name
