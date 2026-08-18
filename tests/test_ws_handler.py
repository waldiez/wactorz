"""The `/ws` endpoint: what a browser is told on connect, and what it may ask for.

Coverage here is deliberately ahead of the authentication work, which lands in
this handler. An unprotected endpoint that nothing describes is one where adding
a guard is indistinguishable from breaking a feature, so these tests pin the
current contract for the guard to fail against.

Real aiohttp throughout — a real server, a real WebSocket client. The framework
is a hard dependency and faking it in `sys.modules` has produced order-dependent
breakage before, so nothing here stubs it.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import events, runtime, ws


class _Registry:
    """Present but empty — chat is offered, and the snapshot has no actors."""

    def all_actors(self) -> list[Any]:
        return []


class _Db:
    """Records chat_log writes instead of holding a database."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_chat_log(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture(name="db")
def db_fixture() -> Iterator[_Db]:
    """Isolate the module-global runtime the handler reads and mutates.

    Yields the double itself rather than `runtime.db`, which is typed
    `WactorzDB | None` — tests assert against the recorder, not the global.
    """
    saved = (runtime.db, runtime.registry, runtime.mqtt_connected, set(runtime.ws_clients))
    recorder = _Db()
    runtime.ws_clients.clear()
    runtime.db = recorder  # pyright: ignore[reportAttributeAccessIssue]
    runtime.registry = _Registry()  # present, so chat is not refused
    runtime.mqtt_connected = True
    yield recorder
    runtime.db, runtime.registry, runtime.mqtt_connected = saved[0], saved[1], saved[2]
    runtime.ws_clients.clear()
    runtime.ws_clients.update(saved[3])


@pytest.fixture(name="client")
async def client_fixture(db: _Db) -> AsyncIterator[TestClient[Any, Any]]:
    """A real server serving the real handler at /ws."""
    app = web.Application()
    app.router.add_get("/ws", ws.ws_handler)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def _frames(socket: Any, count: int) -> list[dict[str, Any]]:
    """The next `count` frames, decoded."""
    out: list[dict[str, Any]] = []
    for _ in range(count):
        msg = await asyncio.wait_for(socket.receive(), timeout=2)
        out.append(json.loads(msg.data))
    return out


async def _opening(socket: Any) -> list[dict[str, Any]]:
    """Everything a browser is sent on connect, before it asks for anything.

    Named rather than a literal count at each call site: most tests here only
    want it out of the way before asserting on what comes next, and when the
    handshake last changed size that count was written out ten times.
    """
    return await _frames(socket, 2)


class TestTheOpeningHandshake:
    async def test_a_browser_is_given_the_state_before_it_asks(
        self, client: TestClient[Any, Any]
    ) -> None:
        # Without this the dashboard is blank until the next broker message,
        # which on a quiet system is a long time to look broken.
        async with client.ws_connect("/ws") as socket:
            first = (await _frames(socket, 1))[0]

        assert first["type"] == "full_snapshot"
        assert "agents" in first["state"]

    async def test_the_broker_connection_state_arrives_immediately(
        self, client: TestClient[Any, Any]
    ) -> None:
        # The "live" badge is wrong on load without it, and nothing else would
        # correct it until the connection state next changed.
        async with client.ws_connect("/ws") as socket:
            frames = await _opening(socket)

        status = [f for f in frames if f["type"] == "mqtt_status"]
        assert status and status[0]["connected"] is True


class TestTheClientRegister:
    async def test_a_connected_browser_is_registered_for_broadcasts(
        self, client: TestClient[Any, Any]
    ) -> None:
        async with client.ws_connect("/ws") as socket:
            await _opening(socket)
            assert len(runtime.ws_clients) == 1

    async def test_a_disconnected_browser_is_forgotten(self, client: TestClient[Any, Any]) -> None:
        # A leak here is invisible until broadcast starts writing to dead
        # sockets, which is a slow failure rather than a loud one.
        async with client.ws_connect("/ws") as socket:
            await _opening(socket)
        for _ in range(50):
            if not runtime.ws_clients:
                break
            await asyncio.sleep(0.01)

        assert not runtime.ws_clients


class TestChatTurnAttribution:
    """A turn is attributed to the agent it addresses, not to the gateway."""

    async def _send(self, client: TestClient[Any, Any], db: _Db, content: str) -> None:
        async with client.ws_connect("/ws") as socket:
            await _opening(socket)
            await socket.send_str(json.dumps({"type": "chat", "content": content}))
            for _ in range(50):
                if db.rows:
                    break
                await asyncio.sleep(0.01)

    async def test_a_mention_attributes_the_turn_to_that_agent(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        with patch.object(ws.chat, "route_chat", new=AsyncMock()):
            await self._send(client, db, "@catalog spawn a weather agent")

        assert db.rows[0]["agent_name"] == "catalog"
        assert db.rows[0]["role"] == "user"

    async def test_a_slash_command_is_attributed_to_main(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        # Matches chat.route_chat's own resolution; without it the turn lands
        # under the io-gateway transport id.
        with patch.object(ws.chat, "route_chat", new=AsyncMock()):
            await self._send(client, db, "/agents list")

        assert db.rows[0]["agent_name"] == "main"

    async def test_the_user_turn_is_stored_before_the_reply_is_attempted(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        # The request must survive an assistant reply that errors out.
        with patch.object(ws.chat, "route_chat", new=AsyncMock(side_effect=RuntimeError("boom"))):
            await self._send(client, db, "@catalog hello")

        assert next(r["role"] for r in db.rows) == "user"

    async def test_credentials_are_redacted_before_they_reach_the_log(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        # chat_log outlives the conversation and is readable through the API, so
        # a user typing a secret must not leave it there in the clear.
        with patch.object(ws.chat, "route_chat", new=AsyncMock()):
            await self._send(client, db, "@main use password=hunter2 please")

        assert "hunter2" not in db.rows[0]["content"]

    async def test_an_empty_message_does_nothing_at_all(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        route = AsyncMock()
        with patch.object(ws.chat, "route_chat", new=route):
            async with client.ws_connect("/ws") as socket:
                await _opening(socket)
                await socket.send_str(json.dumps({"type": "chat", "content": "   "}))
                await asyncio.sleep(0.05)

        route.assert_not_called()
        assert not db.rows

    async def test_a_malformed_frame_does_not_drop_the_connection(
        self, client: TestClient[Any, Any]
    ) -> None:
        # A browser sending nonsense must not cost the user their dashboard.
        async with client.ws_connect("/ws") as socket:
            await _opening(socket)
            await socket.send_str("not json at all")
            await socket.send_str(json.dumps({"type": "command"}))  # no agent_id
            await asyncio.sleep(0.05)

            assert not socket.closed


class TestChatWithoutARegistry:
    async def test_the_user_is_told_rather_than_ignored(self, client: TestClient[Any, Any]) -> None:
        runtime.registry = None
        async with client.ws_connect("/ws") as socket:
            await _opening(socket)
            await socket.send_str(json.dumps({"type": "chat", "content": "hello"}))
            reply = (await _frames(socket, 1))[0]

        assert reply["type"] == "chat"
        assert "Chat unavailable" in reply["content"]


@pytest.fixture(name="commands")
def commands_fixture(db: _Db) -> Iterator[list[dict[str, Any]]]:
    """Patch out everything `handle_command` reaches, recording broadcasts."""
    saved = dict(runtime.state["agents"])
    runtime.state["agents"].clear()
    runtime.state["agents"]["a1"] = {"name": "worker", "state": "running"}
    sent: list[dict[str, Any]] = []

    async def _record(msg: dict[str, Any]) -> None:
        sent.append(msg)

    with (
        patch.object(ws, "broadcast", new=_record),
        patch.object(ws.lifecycle, "delete_agent", new=AsyncMock(return_value="deleted")),
        patch.object(ws.lifecycle, "dispatch_command", new=AsyncMock(return_value=True)),
    ):
        yield sent
    runtime.state["agents"].clear()
    runtime.state["agents"].update(saved)


class TestHandleCommand:
    """The allow-list is the whole authorisation story on this path today."""

    @pytest.mark.parametrize(
        "cmd",
        [
            {"command": "stop"},  # no agent_id
            {"agent_id": "a1"},  # no command
            {"command": "rm -rf", "agent_id": "a1"},  # not on the allow-list
            {"command": "eval", "agent_id": "a1"},
        ],
    )
    async def test_a_command_it_does_not_recognise_does_nothing(
        self, commands: list[dict[str, Any]], cmd: dict[str, Any]
    ) -> None:
        await ws.handle_command(cmd)

        assert not commands
        ws.lifecycle.dispatch_command.assert_not_called()

    @pytest.mark.parametrize(
        ("command", "expected"),
        [("stop", "stopped"), ("pause", "paused"), ("start", "running"), ("resume", "running")],
    )
    async def test_the_dashboard_is_told_the_new_state(
        self, commands: list[dict[str, Any]], command: str, expected: str
    ) -> None:
        await ws.handle_command({"command": command, "agent_id": "a1"})

        assert runtime.state["agents"]["a1"]["state"] == expected
        assert commands[-1]["type"] == "patch"

    async def test_a_refused_command_leaves_the_view_alone(
        self, commands: list[dict[str, Any]]
    ) -> None:
        # Reflecting a state the agent never entered would show it as paused
        # while it is still running, with nothing to correct that until the next
        # heartbeat.
        ws.lifecycle.dispatch_command.return_value = False

        await ws.handle_command({"command": "pause", "agent_id": "a1"})

        assert runtime.state["agents"]["a1"]["state"] == "running"
        assert not commands

    async def test_deleting_a_protected_agent_does_not_remove_it_from_the_dashboard(
        self, commands: list[dict[str, Any]]
    ) -> None:
        # It is still running; broadcasting would clear it from every open
        # dashboard with nothing to put it back.
        ws.lifecycle.delete_agent.return_value = "refused-protected"

        await ws.handle_command({"command": "delete", "agent_id": "a1"})

        assert not commands

    async def test_a_delete_names_the_agent_so_clients_can_act_on_it(
        self, commands: list[dict[str, Any]]
    ) -> None:
        await ws.handle_command({"command": "delete", "agent_id": "a1"})

        assert commands[-1]["type"] == events.DELETE_AGENT_FRAME
        assert commands[-1]["agent_id"] == "a1"

    async def test_a_failure_downstream_does_not_escape_into_the_socket(
        self, commands: list[dict[str, Any]]
    ) -> None:
        # This runs on the receive loop; raising here would drop the connection.
        ws.lifecycle.dispatch_command.side_effect = RuntimeError("boom")

        await ws.handle_command({"command": "stop", "agent_id": "a1"})

        assert not commands


class TestAttachmentsOnATurn:
    """Ids travel; the record of what they are stays server-side."""

    async def test_they_are_stored_against_the_turn(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        stored = {"id": "a" * 32, "name": "shot.png", "mime": "image/png", "size": 12}
        with (
            patch.object(ws.chat, "route_chat", new=AsyncMock()),
            patch.object(ws.uploads, "resolve", return_value=[stored]),
        ):
            async with client.ws_connect("/ws") as socket:
                await _opening(socket)
                await socket.send_str(
                    json.dumps({"type": "chat", "content": "look", "attachments": ["x"]})
                )
                for _ in range(50):
                    if db.rows:
                        break
                    await asyncio.sleep(0.01)

        assert db.rows[0]["attachments"] == [stored]

    async def test_what_the_message_claims_is_not_what_is_recorded(
        self, client: TestClient[Any, Any], db: _Db
    ) -> None:
        # The point of resolving server-side: a caller knows an id, but the
        # name and type are ours. Taking them from the message would let a turn
        # label a file as anything it liked in every thread that shows it.
        with (
            patch.object(ws.chat, "route_chat", new=AsyncMock()),
            patch.object(ws.uploads, "resolve", return_value=[]) as resolve,
        ):
            async with client.ws_connect("/ws") as socket:
                await _opening(socket)
                await socket.send_str(
                    json.dumps(
                        {
                            "type": "chat",
                            "content": "hi",
                            "attachments": [{"id": "x", "name": "invoice.pdf"}],
                        }
                    )
                )
                for _ in range(50):
                    if db.rows:
                        break
                    await asyncio.sleep(0.01)

        resolve.assert_called_once_with([{"id": "x", "name": "invoice.pdf"}])
        assert db.rows[0]["attachments"] == []
