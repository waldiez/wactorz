"""The Discord bot: who it answers, and how.

It fails closed: without an allow-list it does not log in at all, because a bot
that answers anyone spends the LLM budget and controls the house for them. Once
running it answers only a mention, only from an allowed user, only in its
channel when one is set, through the orchestrator as a social channel, and in
pieces short enough for Discord's message limit.

Driven against a stand-in `discord` module, so neither the library nor a
network is needed.
"""

import sys
import types
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from wactorz.core.outbound import Answer
from wactorz.interfaces.chat.discord import DiscordInterface
from wactorz.interfaces.chat.outbound import Outbox


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.dms: list[str] = []

    async def send(self, text: str) -> None:
        self.dms.append(text)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _User) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def mentioned_in(self, message: "_Message") -> bool:
        return f"<@{self.id}>" in message.content or f"<@!{self.id}>" in message.content


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def typing(self) -> _Typing:
        return _Typing()


class _Message:
    def __init__(self, author: _User, content: str, channel: _Channel) -> None:
        self.author = author
        self.content = content
        self.channel = channel


class _Client:
    """Runs the registered handlers over scripted messages when started."""

    #: Every client built, and the messages the next one delivers. Class-level
    #: because the interface builds its own client; reset by the fixture.
    instances: "list[_Client]" = []  # noqa: RUF012  # reset per test by the fixture
    next_script: "list[_Message]" = []  # noqa: RUF012  # reset per test by the fixture
    #: The people the next client can look up, by id; reset by the fixture.
    people: "dict[int, _User]" = {}  # noqa: RUF012  # reset per test by the fixture
    #: What to do once logged in, before the script runs: where a test sends.
    once_logged_in: Any = None

    def __init__(self, intents: Any) -> None:
        self.intents = intents
        self.user: _User | None = None
        self.handlers: dict[str, Any] = {}
        self.script = list(_Client.next_script)
        _Client.instances.append(self)

    def event(self, handler: Any) -> Any:
        self.handlers[handler.__name__] = handler
        return handler

    def get_user(self, user_id: int) -> _User | None:
        return None  # never cached, so `send` has to fetch

    async def fetch_user(self, user_id: int) -> _User:
        try:
            return _Client.people[user_id]
        except KeyError:
            raise LookupError(f"no such user {user_id}") from None

    async def start(self, token: str) -> None:
        self.token = token
        if self.script:
            await self.handlers["on_message"](self.script[0])  # before login: ignored
        self.user = _User(1)
        await self.handlers["on_ready"]()
        if _Client.once_logged_in is not None:
            await _Client.once_logged_in()
        for message in self.script:
            await self.handlers["on_message"](message)


class _Orchestrator:
    """Records each turn's text and where it was said to come from."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.channels: list[tuple[str, str | None]] = []
        self.reply = "ok"

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        self.asked.append(text)
        self.channels.append((channel, user))
        return self.reply

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        yield await self.handle_turn(text, channel=channel, user=user)

    def commands(self) -> frozenset[str]:
        return frozenset()


@pytest.fixture(name="discord_module")
def discord_module_fixture(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("discord")
    module.__dict__.update(
        Intents=types.SimpleNamespace(default=lambda: types.SimpleNamespace(message_content=False)),
        Client=_Client,
        Message=_Message,
    )
    monkeypatch.setitem(sys.modules, "discord", module)
    _Client.instances = []
    _Client.next_script = []
    _Client.people = {}
    _Client.once_logged_in = None
    return module


def _bot(main: _Orchestrator, **kwargs: Any) -> DiscordInterface:
    return DiscordInterface(main, token="tok", **kwargs)


async def test_without_the_library_it_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "discord", None)

    await _bot(_Orchestrator(), allowed_user_ids={7}).run()


async def test_without_an_allow_list_it_does_not_log_in(discord_module: types.ModuleType) -> None:
    await _bot(_Orchestrator()).run()

    assert _Client.instances == []


async def test_only_a_mention_from_an_allowed_user_in_its_channel_is_answered(
    discord_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _Orchestrator()
    main.reply = "x" * 4500
    bot = _bot(main, channel_id=10, allowed_user_ids={7})
    home, elsewhere = _Channel(10), _Channel(11)
    me = _User(1)
    script = [
        _Message(_User(7), "<@1> turn on the lamp", home),
        _Message(me, "<@1> talking to myself", home),
        _Message(_User(7), "<@1> wrong channel", elsewhere),
        _Message(_User(7), "no mention", home),
        _Message(_User(9), "<@!1> not allowed", home),
    ]
    _Client.next_script = script

    await bot.run()

    (client,) = _Client.instances
    assert client.intents.message_content is True
    assert main.asked == ["turn on the lamp"]
    # A public endpoint: the orchestrator is told so, and who is asking.
    assert main.channels == [("social", "7")]
    assert [len(chunk) for chunk in home.sent] == [2000, 2000, 500]
    assert elsewhere.sent == []


async def test_a_throttled_user_is_told_and_not_answered(
    discord_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _Orchestrator()
    bot = _bot(main, allowed_user_ids={7})
    monkeypatch.setattr(bot.limiter, "check", lambda sender: "slow down")
    channel = _Channel(10)
    _Client.next_script = [_Message(_User(7), "<@1> hi", channel)]

    await bot.run()

    assert channel.sent == ["slow down"]
    assert main.asked == []


class _Outbox:
    """Records what the bot registers and the answers it hands over."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.answers: list[Answer] = []

    def register(self, name: str, sender: Any) -> None:
        self.registered.append(name)

    def unregister(self, name: str) -> None:
        self.registered.remove(name)

    async def answered(self, answer: Answer) -> bool:
        self.answers.append(answer)
        return True


async def test_a_message_from_an_agent_is_a_direct_message_with_its_options_numbered(
    discord_module: types.ModuleType,
) -> None:
    outbox = _Outbox()
    bot = _bot(_Orchestrator(), allowed_user_ids={7}, outbox=cast(Outbox, outbox))
    person = _User(7)
    _Client.people = {7: person}
    sent: list[str] = []

    async def _send() -> None:
        sent.append(
            await bot.send(
                "7",
                "Changed?",
                ["maintenance", "real problem"],
                message_id="m1",
                agent_id="agent-1",
            )
        )

    _Client.once_logged_in = _send

    await bot.run()

    assert sent == ["m1"]
    assert person.dms == ["Changed?\n\n1. maintenance\n2. real problem\n\nReply with the number."]
    assert outbox.registered == []  # forgotten once the bot stopped


async def test_a_numbered_reply_answers_the_agent_and_is_not_a_turn(
    discord_module: types.ModuleType,
) -> None:
    outbox = _Outbox()
    main = _Orchestrator()
    bot = _bot(main, allowed_user_ids={7}, outbox=cast(Outbox, outbox))
    person = _User(7)
    _Client.people = {7: person}
    home = _Channel(10)

    async def _ask() -> None:
        await bot.send(
            "7", "Changed?", ["maintenance", "real problem"], message_id="m1", agent_id="agent-1"
        )

    _Client.once_logged_in = _ask
    _Client.next_script = [
        _Message(_User(9), "2", home),  # not allowed: not an answer, not a turn
        _Message(_User(7), "2", home),  # the answer, no mention needed
        _Message(_User(7), "<@1> 2", home),  # answered already: a turn like any other
    ]

    await bot.run()

    (answer,) = outbox.answers
    assert (
        answer.id,
        answer.agent_id,
        answer.option,
        answer.index,
        answer.channel,
        answer.user,
    ) == (
        "m1",
        "agent-1",
        "real problem",
        1,
        "discord",
        "7",
    )
    assert main.asked == ["2"]


async def test_a_long_direct_message_is_split(discord_module: types.ModuleType) -> None:
    bot = _bot(_Orchestrator(), allowed_user_ids={7})
    person = _User(7)
    _Client.people = {7: person}

    async def _send() -> None:
        await bot.send("7", "x" * 4500)

    _Client.once_logged_in = _send

    await bot.run()

    assert [len(piece) for piece in person.dms] == [2000, 2000, 500]


async def test_before_the_bot_runs_there_is_nothing_to_send_with(
    discord_module: types.ModuleType,
) -> None:
    bot = _bot(_Orchestrator(), allowed_user_ids={7})
    with pytest.raises(RuntimeError, match="not running"):
        await bot.send("7", "hi")


async def test_a_user_that_is_not_an_id_is_refused(discord_module: types.ModuleType) -> None:
    bot = _bot(_Orchestrator(), allowed_user_ids={7})
    seen: list[BaseException] = []

    async def _send() -> None:
        try:
            await bot.send("alice", "hi")
        except ValueError as exc:
            seen.append(exc)

    _Client.once_logged_in = _send

    await bot.run()

    assert len(seen) == 1
    assert "numeric id" in str(seen[0])
