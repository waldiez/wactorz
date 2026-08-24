"""The Telegram bot: who it answers, and what it refuses.

Everything here is an access-control claim, because that is what this interface
is. A Telegram bot is a public endpoint — anyone who finds it can message it —
so the interesting behaviour is all in the paths that end without reaching the
agent: an unlisted user, an unconfigured bot, a caller over the rate limit.

**`python-telegram-bot` is faked here rather than imported.** It is an optional
extra: CI installs `.[all,dev]` and gets it, but `pip install -e ".[dev]"` — the
natural thing for a contributor to run — does not. A test resting on the real
package would pass here and fail there. Faking the three modules `run()` imports
also pins the tests to *our* use of that API rather than to its current shape.

`start_cmd` and `handle_message` are closures inside `run()`, so there is nothing
to import: the tests take them from the fake `Application` the way the library
would, which keeps the handler under test the one the real code path builds.
"""

import asyncio
import contextlib
import logging
import sys
import types
from typing import Any

import pytest

from wactorz.interfaces.chat.telegram import TelegramInterface


class _Recorder:
    """A main actor that records what it was asked, and answers a fixed string."""

    def __init__(self, reply: str = "an answer") -> None:
        self.reply = reply
        self.asked: list[str] = []

    async def process_user_input_restricted(self, text: str) -> str:
        self.asked.append(text)
        return self.reply


class _Message:
    def __init__(self, text: str = "hello") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class _User:
    def __init__(self, uid: int, username: str = "someone") -> None:
        self.id = uid
        self.username = username
        self.first_name = username


class _Chat:
    def __init__(self, cid: int = 99) -> None:
        self.id = cid


class _Update:
    """The four attributes the handlers read off an update."""

    def __init__(
        self,
        user: _User | None = None,
        message: _Message | None = None,
        chat: _Chat | None = None,
    ) -> None:
        self.effective_user = user
        self.message = message
        self.effective_chat = chat


class _Bot:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))


class _Context:
    def __init__(self) -> None:
        self.bot = _Bot()


#: Every fake Application built during a test. Module-level rather than a class
#: attribute: the library constructs these itself through `builder()`, so there
#: is nowhere to inject a registry.
_BUILT: list["_Application"] = []


class _Application:
    """Stands in for the library's Application, and keeps the handlers."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        _BUILT.append(self)

    # ── builder chain ───────────────────────────────────────────────────────
    @classmethod
    def builder(cls) -> "_Application":
        return cls()

    def token(self, _token: str) -> "_Application":
        return self

    def build(self) -> "_Application":
        return self

    # ── registration ────────────────────────────────────────────────────────
    def add_handler(self, handler: Any) -> None:
        self.handlers[handler.kind] = handler.callback

    async def initialize(self) -> None:
        return None

    async def start(self) -> None:
        return None

    @property
    def updater(self) -> None:
        """No updater, so `run()` never starts polling and returns instead."""
        return None


class _CommandHandler:
    def __init__(self, _name: str, callback: Any) -> None:
        self.kind = "command"
        self.callback = callback


class _MessageHandler:
    def __init__(self, _filters: Any, callback: Any) -> None:
        self.kind = "message"
        self.callback = callback


class _Filters:
    """Enough of the library's filter algebra for `TEXT & ~COMMAND` to evaluate."""

    def __and__(self, _other: "_Filters") -> "_Filters":
        return self

    def __invert__(self) -> "_Filters":
        return self


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the three modules `run()` imports into `sys.modules`, restorably.

    `monkeypatch.setitem` rather than a bare assignment: `sys.modules` is
    process-wide and never restored on its own, so an unrestored stub shadows the
    real package for every later test in the session.
    """
    telegram = types.ModuleType("telegram")
    telegram.Update = _Update  # type: ignore[attr-defined]

    constants = types.ModuleType("telegram.constants")
    constants.ChatAction = types.SimpleNamespace(TYPING="typing")  # type: ignore[attr-defined]

    ext = types.ModuleType("telegram.ext")
    ext.Application = _Application  # type: ignore[attr-defined]
    ext.CommandHandler = _CommandHandler  # type: ignore[attr-defined]
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)  # type: ignore[attr-defined]
    ext.MessageHandler = _MessageHandler  # type: ignore[attr-defined]

    class _FilterHolder:
        TEXT = _Filters()
        COMMAND = _Filters()

    ext.filters = _FilterHolder  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)


async def _handlers(interface: TelegramInterface) -> dict[str, Any]:
    """Run `run()` far enough to register the handlers, then hand them back.

    As a cancellable task, because `run()` never returns: it ends on
    `await asyncio.Event().wait()`, which is how the bot stays up for the life of
    the process. Everything before that point is non-blocking, so yielding a few
    times is enough to get past registration.
    """
    _BUILT.clear()
    task = asyncio.create_task(interface.run())
    for _ in range(100):
        if _BUILT and _BUILT[0].handlers:
            break
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert _BUILT, "run() never registered its handlers"
    return _BUILT[0].handlers


@pytest.fixture(name="fake_telegram", autouse=True)
def fake_telegram_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_telegram(monkeypatch)


@pytest.fixture(name="actor")
def actor_fixture() -> _Recorder:
    return _Recorder()


class TestWhenTheLibraryIsMissing:
    async def test_it_logs_and_returns_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch, actor: _Recorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The extra is optional, so a missing one must not take the process down.

        `run()` is gathered alongside the actor system, so raising here would
        stop agents that have nothing to do with Telegram.
        """
        monkeypatch.setitem(sys.modules, "telegram.ext", None)

        with caplog.at_level(logging.ERROR):
            await TelegramInterface(actor, token="t", allowed_user_ids=[1]).run()  # type: ignore[arg-type]

        assert "python-telegram-bot" in caplog.text


class TestWhoItAnswers:
    async def test_an_allowed_user_reaches_the_agent(self, actor: _Recorder) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message("what is the weather")

        await handlers["message"](_Update(_User(7), message, _Chat()), _Context())

        assert actor.asked == ["what is the weather"]
        assert message.replies == ["an answer"]

    async def test_an_unlisted_user_is_ignored_silently(
        self, actor: _Recorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No reply at all, deliberately: answering an unknown sender confirms
        the bot is live and worth probing."""
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        with caplog.at_level(logging.WARNING):
            await handlers["message"](_Update(_User(999), message, _Chat()), _Context())

        assert not actor.asked
        assert not message.replies
        assert "Rejected" in caplog.text

    async def test_it_uses_the_restricted_surface(self, actor: _Recorder) -> None:
        """A public endpoint must not reach the surface that can spawn agents or
        run code — `_Recorder` only implements the restricted call, so reaching
        for the other one fails loudly rather than silently widening access."""
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)

        await handlers["message"](_Update(_User(7), _Message("hi"), _Chat()), _Context())

        assert actor.asked == ["hi"]

    async def test_the_singular_allowed_id_is_folded_in(self, actor: _Recorder) -> None:
        """The older single-user setting still has to grant access, or upgrading
        silently locks the one configured user out."""
        interface = TelegramInterface(actor, token="t", allowed_user_id=7)  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["message"](_Update(_User(7), message, _Chat()), _Context())

        assert actor.asked == ["hello"]


class TestSetupMode:
    async def test_start_reports_the_user_id_when_nothing_is_configured(
        self, actor: _Recorder
    ) -> None:
        """With no allow-list the bot is a way to *learn* your id, and nothing
        more — it is the only thing it will do until one is configured."""
        interface = TelegramInterface(actor, token="t")  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["command"](_Update(_User(4242), message, _Chat()), _Context())

        assert "4242" in message.replies[0]
        assert "TELEGRAM_ALLOWED_USER_IDS" in message.replies[0]

    async def test_an_unconfigured_bot_answers_no_message(self, actor: _Recorder) -> None:
        """It says so rather than going quiet — an unconfigured bot that ignores
        everything is indistinguishable from a broken one."""
        interface = TelegramInterface(actor, token="t")  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message("do something")

        await handlers["message"](_Update(_User(1), message, _Chat()), _Context())

        assert not actor.asked
        assert "/start" in message.replies[0]

    async def test_start_greets_once_configured(self, actor: _Recorder) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["command"](_Update(_User(7, "ada"), message, _Chat()), _Context())

        assert "online" in message.replies[0]


class TestMalformedUpdates:
    @pytest.mark.parametrize(
        ("update", "label"),
        [
            (_Update(_User(7), None, _Chat()), "no message"),
            (_Update(_User(7), _Message(""), _Chat()), "empty text"),
            (_Update(None, _Message("hi"), _Chat()), "no user"),
        ],
    )
    async def test_it_returns_instead_of_raising(
        self, actor: _Recorder, update: _Update, label: str
    ) -> None:
        """Telegram sends updates for edits, joins and service events; a handler
        that assumes a text message from a known user raises on all of them."""
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)

        await handlers["message"](update, _Context())

        assert not actor.asked, label

    async def test_a_missing_chat_stops_before_the_typing_action(self, actor: _Recorder) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        context = _Context()

        await handlers["message"](_Update(_User(7), _Message("hi"), None), context)

        assert not context.bot.actions
        assert not actor.asked

    async def test_start_without_a_message_returns(self, actor: _Recorder) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)

        await handlers["command"](_Update(_User(7), None, _Chat()), _Context())


class TestThrottlingAndLongReplies:
    async def test_a_throttled_sender_is_told_and_not_forwarded(
        self, actor: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        monkeypatch.setattr(interface.limiter, "check", lambda _s: "slow down")
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["message"](_Update(_User(7), message, _Chat()), _Context())

        assert message.replies == ["slow down"]
        assert not actor.asked

    async def test_the_slot_is_released_even_when_the_agent_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Released in a `finally`, or one failed turn throttles that user for
        good — the limiter would still be holding a slot nothing will return."""

        class _Boom:
            async def process_user_input_restricted(self, _text: str) -> str:
                raise RuntimeError("model unavailable")

        interface = TelegramInterface(_Boom(), token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        released: list[str] = []
        monkeypatch.setattr(interface.limiter, "done", lambda s: released.append(s))
        handlers = await _handlers(interface)

        with pytest.raises(RuntimeError):
            await handlers["message"](_Update(_User(7), _Message("hi"), _Chat()), _Context())

        assert released == ["7"]

    async def test_an_empty_answer_still_says_something(self, actor: _Recorder) -> None:
        actor.reply = ""
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["message"](_Update(_User(7), message, _Chat()), _Context())

        assert message.replies == ["(no response)"]

    async def test_a_long_answer_is_split_into_telegram_sized_pieces(
        self, actor: _Recorder
    ) -> None:
        """Telegram refuses a message over 4096 characters, so an unsplit long
        answer is not truncated — it is rejected, and the user gets nothing."""
        actor.reply = "x" * 9000
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        message = _Message()

        await handlers["message"](_Update(_User(7), message, _Chat()), _Context())

        assert [len(part) for part in message.replies] == [4096, 4096, 808]
        assert "".join(message.replies) == actor.reply

    async def test_a_typing_action_is_sent_before_the_answer(self, actor: _Recorder) -> None:
        interface = TelegramInterface(actor, token="t", allowed_user_ids=[7])  # type: ignore[arg-type]
        handlers = await _handlers(interface)
        context = _Context()

        await handlers["message"](_Update(_User(7), _Message("hi"), _Chat(55)), context)

        assert context.bot.actions == [(55, "typing")]
