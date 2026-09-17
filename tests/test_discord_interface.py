"""The Discord bot: who it answers, and how.

It fails closed: without an allow-list it does not log in at all, because a bot
that answers anyone spends the LLM budget and controls the house for them. Once
running it answers only a mention, only from an allowed user, only in its
channel when one is set, through main's restricted path, and in pieces short
enough for Discord's message limit.

Driven against a stand-in `discord` module, so neither the library nor a
network is needed.
"""

import sys
import types
from typing import Any, cast

import pytest

from wactorz.agents.main import MainActor
from wactorz.interfaces.chat.discord import DiscordInterface


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id

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

    def __init__(self, intents: Any) -> None:
        self.intents = intents
        self.user: _User | None = None
        self.handlers: dict[str, Any] = {}
        self.script = list(_Client.next_script)
        _Client.instances.append(self)

    def event(self, handler: Any) -> Any:
        self.handlers[handler.__name__] = handler
        return handler

    async def start(self, token: str) -> None:
        self.token = token
        await self.handlers["on_message"](self.script[0])  # before login: ignored
        self.user = _User(1)
        await self.handlers["on_ready"]()
        for message in self.script:
            await self.handlers["on_message"](message)


class _Main:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.reply = "ok"

    async def process_user_input_restricted(self, text: str) -> str:
        self.asked.append(text)
        return self.reply


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
    return module


def _bot(main: _Main, **kwargs: Any) -> DiscordInterface:
    return DiscordInterface(cast(MainActor, main), token="tok", **kwargs)


async def test_without_the_library_it_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "discord", None)

    await _bot(_Main(), allowed_user_ids={7}).run()


async def test_without_an_allow_list_it_does_not_log_in(discord_module: types.ModuleType) -> None:
    await _bot(_Main()).run()

    assert _Client.instances == []


async def test_only_a_mention_from_an_allowed_user_in_its_channel_is_answered(
    discord_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _Main()
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
    assert [len(chunk) for chunk in home.sent] == [2000, 2000, 500]
    assert elsewhere.sent == []


async def test_a_throttled_user_is_told_and_not_answered(
    discord_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _Main()
    bot = _bot(main, allowed_user_ids={7})
    monkeypatch.setattr(bot.limiter, "check", lambda sender: "slow down")
    channel = _Channel(10)
    _Client.next_script = [_Message(_User(7), "<@1> hi", channel)]

    await bot.run()

    assert channel.sent == ["slow down"]
    assert main.asked == []
