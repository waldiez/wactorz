"""`wactorz.client` against the real monitor application.

Nothing of the server is faked but what a turn says: the routes, the socket, the
key and the uploads are the real ones, so a change to a route or a frame fails
here. `route_chat` is replaced because an agent's words are not what is under test.
"""

import ast
import asyncio
import dataclasses
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

from tests.waiting import PATIENCE_S, until
from wactorz import client as client_package
from wactorz import config as config_module
from wactorz.client import (
    Attachment,
    Client,
    Disconnected,
    Dropped,
    EmptyMessageError,
    Frame,
    NoServerError,
    NotFoundError,
    NotOfferedError,
    Part,
    Reconnected,
    RejectedError,
    TimedOutError,
    TurnInterruptedError,
    UnauthorizedError,
    UnavailableError,
)
from wactorz.client import chat as chat_module
from wactorz.client import stream as stream_module
from wactorz.client.stream import Listener
from wactorz.core.identity import derive_actor_id
from wactorz.ext import tts
from wactorz.web import answers, chat, questions, runtime, uploads
from wactorz.web.app import build_app

KEY = "a-key-long-enough-for-these-tests"
Route = Callable[..., Awaitable[None]]


class _Registry:
    def all_actors(self) -> list[Any]:
        return []

    def find_by_name(self, name: str) -> Any:
        known = ("main", "catalog")
        return SimpleNamespace(name=name, actor_id=derive_actor_id(name)) if name in known else None


class _Db:
    """The chat log, as far as the routes and the socket use it."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_chat_log(self, **row: Any) -> None:
        self.rows.append(row)

    def query_chat_log(self, **_query: Any) -> list[dict[str, Any]]:
        return [{"role": row["role"], "content": row["content"]} for row in self.rows]


async def _no_voices(_app: Any = None) -> None:
    """In place of warming the speech voices, which goes to the network."""


@pytest.fixture(name="db")
def db_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Db:
    recorder = _Db()
    monkeypatch.setattr(runtime, "db", recorder)
    monkeypatch.setattr(runtime, "registry", _Registry())
    monkeypatch.setattr(runtime, "mqtt_connected", True)
    monkeypatch.setattr(runtime, "ws_clients", set())
    monkeypatch.setattr(
        config_module, "CONFIG", dataclasses.replace(config_module.CONFIG, api_key=KEY)
    )
    monkeypatch.setattr(stream_module, "RECONNECT_FIRST_S", 0.01)
    monkeypatch.setattr(uploads, "resolve_state_dir", lambda _given=None: str(tmp_path))
    monkeypatch.setattr(tts, "_warm_tts_voices", _no_voices)
    return recorder


@pytest.fixture(name="server")
async def server_fixture(db: _Db) -> AsyncIterator[TestServer]:
    server = TestServer(build_app())
    await server.start_server()
    yield server
    await server.close()


@pytest.fixture(name="wz")
async def wz_fixture(server: TestServer) -> AsyncIterator[Client]:
    async with Client(str(server.make_url("")), api_key=KEY) as client:
        yield client


def _answering(monkeypatch: pytest.MonkeyPatch, route: Route) -> None:
    monkeypatch.setattr(chat, "route_chat", route)


async def _parts_of(wz: Client, text: str, **kwargs: Any) -> list[Part]:
    return [part async for part in wz.chat.send(text, **kwargs)]


async def _said(wz: Client, text: str, **kwargs: Any) -> list[Part]:
    """All a turn says. Bounded, as a turn that never ends is what these tests would find."""
    return await asyncio.wait_for(_parts_of(wz, text, **kwargs), PATIENCE_S)


async def _drop_every_socket() -> None:
    for channel in list(runtime.ws_clients):
        await channel.ws.close()


class TestAskingTheServer:
    async def test_it_says_its_version_and_what_it_offers(self, wz: Client) -> None:
        server = await wz.server()

        assert server.version
        assert server.raw["version"] == server.version

    async def test_a_wrong_key_says_so(self, server: TestServer) -> None:
        async with Client(str(server.make_url("")), api_key="wrong") as wz:
            with pytest.raises(UnauthorizedError):
                await wz.server()

    async def test_a_route_the_server_lacks_says_so_rather_than_returning_a_page(
        self, wz: Client
    ) -> None:
        with pytest.raises(NotOfferedError):
            await wz.request("GET", "/api/nothing-of-the-kind")

    async def test_a_thing_that_is_not_there_is_not_a_missing_route(self, wz: Client) -> None:
        with pytest.raises(NotFoundError):
            await wz.files.download("0" * 32)

    async def test_a_server_that_is_gone_is_unavailable(self) -> None:
        async with Client("http://127.0.0.1:9") as wz:
            with pytest.raises(UnavailableError):
                await wz.server()

    async def test_a_change_that_timed_out_may_have_happened(self) -> None:
        error = TimedOutError("POST", "/api/reset", 1)

        assert error.may_have_happened
        assert isinstance(error, asyncio.TimeoutError)
        assert not TimedOutError("GET", "/api/actors", 1).may_have_happened


class TestATurn:
    async def test_a_streamed_reply_arrives_in_pieces_and_ends(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def route(
            _text: str, _reply: Any, stream_fn: Any, stream_end_fn: Any, **_: Any
        ) -> None:
            await stream_fn("Hel")
            await stream_fn("lo")
            await stream_end_fn()

        _answering(monkeypatch, route)

        parts = await _said(wz, "hi")

        assert [(part.text, part.streaming) for part in parts] == [("Hel", True), ("lo", True)]
        assert parts[0].agent == "main"

    async def test_a_whole_reply_ends_too_though_it_has_no_end_of_its_own(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def route(_text: str, reply: Any, **_: Any) -> None:
            await reply("first")
            await reply("second")

        _answering(monkeypatch, route)

        parts = await _said(wz, "/agents")

        assert [(part.text, part.streaming) for part in parts] == [
            ("first", False),
            ("second", False),
        ]

    async def test_an_error_in_the_turn_is_said_and_ends_it(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def route(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the model is away")

        _answering(monkeypatch, route)

        parts = await _said(wz, "hi")

        assert [part.text for part in parts] == ["[error] the model is away"]

    async def test_overlapping_turns_of_one_agent_stay_apart(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first_may_finish = asyncio.Event()

        async def route(text: str, reply: Any, **_: Any) -> None:
            if text == "slow":
                await first_may_finish.wait()
            await reply(f"answer to {text}")

        _answering(monkeypatch, route)

        slow = asyncio.create_task(_said(wz, "slow"))
        await until(lambda: chat.inflight_chat_tasks, "the first turn to start")
        quick = await _said(wz, "quick")
        first_may_finish.set()

        assert [part.text for part in quick] == ["answer to quick"]
        assert [part.text for part in await slow] == ["answer to slow"]

    async def test_a_message_for_another_agent_goes_as_a_mention(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        heard: list[str] = []

        async def route(text: str, reply: Any, **_: Any) -> None:
            heard.append(text)
            await reply("ok")

        _answering(monkeypatch, route)

        parts = await _said(wz, "list", to="catalog")

        assert heard == ["@catalog list"]
        assert parts[0].agent == "catalog"

    async def test_a_name_that_fits_several_offers_the_choices(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def route(_text: str, _reply: Any, choices_fn: Any, **_: Any) -> None:
            await choices_fn("which?", ("w@a", "w@b"))

        _answering(monkeypatch, route)

        parts = await _said(wz, "@w hi")

        assert parts[0].choices == ("w@a", "w@b")

    async def test_a_message_with_no_words_is_refused_here(self, wz: Client) -> None:
        with pytest.raises(EmptyMessageError):
            await _said(wz, "   ")

    async def test_a_dropped_socket_interrupts_the_turn_and_it_is_not_sent_again(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        heard: list[str] = []

        async def route(text: str, reply: Any, **_: Any) -> None:
            heard.append(text)
            await reply("working")
            await asyncio.Event().wait()

        _answering(monkeypatch, route)

        with pytest.raises(TurnInterruptedError):
            async for _part in wz.chat.send("hi"):
                await _drop_every_socket()

        assert heard == ["hi"]

    async def test_a_server_that_never_accepts_the_message_does_not_hang_the_caller(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", None)
        monkeypatch.setattr(chat_module, "ACCEPTED_TIMEOUT_S", 0.05)

        with pytest.raises(TurnInterruptedError):
            await _said(wz, "hi")

    async def test_stopping_says_how_many_turns_it_cancelled(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def route(*_args: Any, **_kwargs: Any) -> None:
            await asyncio.Event().wait()

        _answering(monkeypatch, route)
        turn = asyncio.create_task(_said(wz, "hi"))
        await until(lambda: chat.inflight_chat_tasks, "the turn to start")

        assert await wz.chat.stop_all() == 1
        assert [part.text for part in await turn] == ["⏹ Stopped."]


class TestFiles:
    async def test_a_file_goes_up_with_its_message_and_comes_back(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        got: list[Any] = []

        async def route(_text: str, reply: Any, attachments: Any, **_: Any) -> None:
            got.extend(attachments)
            await reply("seen")

        _answering(monkeypatch, route)
        source = tmp_path / "notes.txt"
        source.write_bytes(b"some words")

        await _said(wz, "read this", attachments=[source])

        assert [file["name"] for file in got] == ["notes.txt"]
        assert await wz.files.download(got[0]["id"]) == b"some words"

    async def test_a_file_already_stored_is_not_sent_twice(
        self, wz: Client, tmp_path: Path
    ) -> None:
        source = tmp_path / "notes.txt"
        source.write_bytes(b"some words")

        stored = await wz.files.upload(source)

        assert stored.size == len(b"some words")
        assert await wz.files.upload(stored) is stored

    async def test_an_empty_file_is_refused_in_the_servers_words(
        self, wz: Client, tmp_path: Path
    ) -> None:
        source = tmp_path / "empty.txt"
        source.touch()

        with pytest.raises(RejectedError, match="empty file"):
            await wz.files.upload(source)

    async def test_a_file_that_is_not_there_is_raised_before_anything_is_sent(
        self, wz: Client, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            await _said(wz, "read this", attachments=[tmp_path / "missing"])

    def test_an_attachment_needs_only_its_id(self) -> None:
        assert Attachment("abc").name == ""


OPEN_QUESTION = {
    "id": "q1",
    "agent_id": derive_actor_id("hvac"),
    "agent": "hvac",
    "text": "Did something change?",
    "options": ["yes", "no"],
    "channel": "dashboard",
    "asked_at": 1.5,
    "answer": None,
}


class TestQuestions:
    async def test_open_and_answered_are_read_from_the_ledger(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answered = {**OPEN_QUESTION, "id": "q2", "answer": "no"}
        monkeypatch.setattr(
            questions, "listing", lambda: {"open": [OPEN_QUESTION], "answered": [answered]}
        )

        waiting = await wz.questions.open()
        done = await wz.questions.answered()

        assert [(q.id, q.options, q.answer) for q in waiting] == [("q1", ("yes", "no"), None)]
        assert [q.answer for q in done] == ["no"]

    async def test_an_answer_reaches_the_agent(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[Any] = []

        async def send(answer: Any) -> None:
            sent.append(answer)

        monkeypatch.setattr(questions, "listing", lambda: {"open": [OPEN_QUESTION], "answered": []})
        monkeypatch.setattr(questions, "checked", lambda answer, **_: answer)
        monkeypatch.setattr(answers, "_send", send)
        monkeypatch.setattr(answers, "vouch", lambda *_: None)
        question = (await wz.questions.open())[0]

        await wz.questions.answer(question, "yes", note="after the filter change")

        assert [(a.id, a.option, a.note) for a in sent] == [
            ("q1", "yes", "after the filter change")
        ]

    async def test_a_question_the_ledger_does_not_know_is_refused(
        self, wz: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(questions, "listing", lambda: {"open": [OPEN_QUESTION], "answered": []})
        question = (await wz.questions.open())[0]

        with pytest.raises((RejectedError, NotFoundError)):
            await wz.questions.answer(question, "yes")


class TestEvents:
    async def test_a_listener_starts_from_the_current_state(self, wz: Client) -> None:
        events = wz.events()

        first = await anext(events)
        await events.aclose()

        assert isinstance(first, Frame)
        assert first.type == "full_snapshot"

    async def test_a_late_listener_starts_from_the_state_too(self, wz: Client) -> None:
        early = wz.events()
        await anext(early)

        late = wz.events()
        first = await anext(late)
        await early.aclose()
        await late.aclose()

        assert isinstance(first, Frame)
        assert first.type == "full_snapshot"

    async def test_a_dropped_socket_is_told_and_comes_back(self, wz: Client) -> None:
        events = wz.events()
        await anext(events)
        await _drop_every_socket()

        seen: list[type] = []
        async for event in events:
            seen.append(type(event))
            if isinstance(event, Reconnected):
                break
        await events.aclose()

        assert Disconnected in seen
        assert seen[-1] is Reconnected

    async def test_a_wrong_key_ends_the_stream_rather_than_retrying_for_ever(
        self, server: TestServer
    ) -> None:
        async with Client(str(server.make_url("")), api_key="wrong") as wz:
            with pytest.raises(UnauthorizedError):
                await anext(wz.events())

    async def test_closing_the_client_ends_its_listeners(self, server: TestServer) -> None:
        wz = Client(str(server.make_url("")), api_key=KEY)
        await wz.open()
        events = wz.events()
        await anext(events)
        rest = asyncio.create_task(anext(events, None))

        await wz.close()

        await asyncio.wait_for(rest, 5)

    async def test_a_listener_behind_loses_its_oldest_and_is_told(self) -> None:
        listener = Listener(limit=2)
        for number in range(5):
            listener.put(Frame(str(number)))

        assert await listener.get() == Dropped(3)
        assert await listener.get() == Frame("3")

    def test_an_unknown_frame_passes_through(self) -> None:
        assert stream_module._parsed('{"type": "from-the-future", "x": 1}') == Frame(
            "from-the-future", {"type": "from-the-future", "x": 1}
        )

    def test_what_is_not_a_frame_is_ignored(self) -> None:
        assert stream_module._parsed("not json") is None
        assert stream_module._parsed("[1]") is None


class TestTheKey:
    def test_it_is_left_out_of_repr(self) -> None:
        wz = Client("http://example.test", api_key="the-secret")

        assert "the-secret" not in repr(wz)
        assert "the-secret" not in repr(wz._transport)

    def test_the_environment_names_the_server(self) -> None:
        wz = Client.from_env({"WACTORZ_URL": "http://example.test/", "WACTORZ_API_KEY": "k"})

        assert wz._transport.url == "http://example.test"

    def test_the_servers_own_key_is_not_taken(self) -> None:
        wz = Client.from_env({"WACTORZ_URL": "http://example.test", "API_KEY": "the-server's"})

        assert wz._transport._api_key == ""

    def test_no_server_named_says_which_variable(self) -> None:
        with pytest.raises(NoServerError, match="WACTORZ_URL"):
            Client.from_env({})

    @pytest.mark.parametrize(
        ("url", "warned"),
        [
            ("http://edge.local:8888", True),
            ("https://edge.local", False),
            ("http://127.0.0.1:8888", False),
            ("http://localhost:8888", False),
        ],
    )
    async def test_a_key_in_clear_across_a_network_is_warned_about(
        self, url: str, warned: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            async with Client(url, api_key="k"):
                pass

        assert ("plain http" in caplog.text) is warned


def test_the_client_holds_the_connection_and_every_other_thing_is_an_area() -> None:
    # A new area adds no method here, so `Client` does not grow with the API.
    public = {name for name in vars(Client) if not name.startswith("_")}

    assert public == {"close", "events", "from_env", "open", "request", "server"}


def test_the_socket_address_follows_the_scheme() -> None:
    assert Client("https://h/base")._transport.websocket_url() == "wss://h/base/ws"
    assert Client("http://h:8888")._transport.websocket_url() == "ws://h:8888/ws"


def test_the_package_imports_only_the_standard_library_and_aiohttp() -> None:
    # What keeps the folder free to become a distribution of its own.
    allowed = {"aiohttp", *sys.stdlib_module_names}
    folder = Path(client_package.__file__).parent

    for path in folder.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            assert not [n for n in names if n.split(".")[0] not in allowed], path.name
