"""Tests for ``wactorz.build_social_companions``.

Social channels (Discord/Telegram) run *alongside* the primary interface when
their token is set — so the HA-addon dashboard (rest) can also expose the bots.
These tests check the selection logic without touching the network: the
interface objects only construct a client on ``.run()``.
"""

from types import SimpleNamespace

import wactorz.interfaces.chat_interfaces as ci
from wactorz.interfaces.chat_interfaces import (
    DiscordInterface,
    TelegramInterface,
    build_social_companions,
    run_all_interfaces,
)


class _DummyMain:
    name = "main"


def _set_tokens(monkeypatch, *, discord="", telegram="", allowed=0):
    # CONFIG is a frozen dataclass, so swap the whole module reference for a fake.
    monkeypatch.setattr(
        ci,
        "CONFIG",
        SimpleNamespace(
            discord_token=discord,
            telegram_token=telegram,
            telegram_allowed_user_id=allowed,
        ),
    )


def test_both_tokens_start_alongside_rest(monkeypatch):
    _set_tokens(monkeypatch, discord="d-tok", telegram="t-tok", allowed=42)

    companions = build_social_companions(_DummyMain(), primary="rest")

    kinds = {type(c) for c in companions}
    assert kinds == {DiscordInterface, TelegramInterface}
    telegram = next(c for c in companions if isinstance(c, TelegramInterface))
    assert telegram.allowed_user_id == 42


def test_no_tokens_no_companions(monkeypatch):
    _set_tokens(monkeypatch)
    assert build_social_companions(_DummyMain(), primary="rest") == []


def test_primary_channel_is_not_duplicated(monkeypatch):
    _set_tokens(monkeypatch, discord="d-tok", telegram="t-tok")

    # Discord is the primary → only Telegram rides along (no double Discord login).
    companions = build_social_companions(_DummyMain(), primary="discord")
    assert [type(c) for c in companions] == [TelegramInterface]

    # …and vice-versa.
    companions = build_social_companions(_DummyMain(), primary="telegram")
    assert [type(c) for c in companions] == [DiscordInterface]


def test_run_all_returns_coroutines(monkeypatch):
    _set_tokens(monkeypatch, telegram="t-tok")
    companions = build_social_companions(_DummyMain(), primary="rest")
    coros = run_all_interfaces(companions)
    assert len(coros) == 1
    # Close the coroutine so it doesn't warn about never being awaited.
    coros[0].close()
