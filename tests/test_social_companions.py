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


def _set_tokens(
    monkeypatch,
    *,
    discord="",
    telegram="",
    discord_allowed=(7,),
    telegram_allowed=(42,),
    deps_present=True,
):
    # CONFIG is a frozen dataclass, so swap the whole module reference for a fake.
    # Both channels get an allow-list by default — without one they refuse to
    # start, which is its own test below.
    monkeypatch.setattr(
        ci,
        "CONFIG",
        SimpleNamespace(
            discord_token=discord,
            telegram_token=telegram,
            discord_allowed_user_ids=frozenset(discord_allowed),
            telegram_allowed_user_ids=frozenset(telegram_allowed),
            social_rate_limit_per_min=12,
        ),
    )
    # Default to "libraries installed" so selection tests don't depend on which
    # optional deps happen to be present in the test environment.
    if deps_present:
        monkeypatch.setattr(ci, "missing_dependency", lambda channel: None)


def test_both_tokens_start_alongside_rest(monkeypatch):
    _set_tokens(monkeypatch, discord="d-tok", telegram="t-tok")

    companions = build_social_companions(_DummyMain(), primary="rest")

    kinds = {type(c) for c in companions}
    assert kinds == {DiscordInterface, TelegramInterface}
    telegram = next(c for c in companions if isinstance(c, TelegramInterface))
    discord = next(c for c in companions if isinstance(c, DiscordInterface))
    assert telegram.allowed_user_ids == frozenset({42})
    assert discord.allowed_user_ids == frozenset({7})


def test_channel_without_allow_list_refuses_to_start(monkeypatch, caplog):
    """A token with no allow-list would answer anyone who finds the bot, which
    means spending the user's LLM budget and controlling their home."""
    import logging

    _set_tokens(
        monkeypatch,
        discord="d-tok",
        telegram="t-tok",
        discord_allowed=(),
        telegram_allowed=(),
    )

    with caplog.at_level(logging.WARNING):
        companions = build_social_companions(_DummyMain(), primary="rest")

    assert companions == []
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Discord companion NOT started" in messages
    assert "Telegram companion NOT started" in messages
    assert "DISCORD_ALLOWED_USER_IDS" in messages
    assert "TELEGRAM_ALLOWED_USER_IDS" in messages


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


def test_missing_library_is_skipped_with_loud_warning(monkeypatch, caplog):
    import logging

    _set_tokens(monkeypatch, telegram="t-tok")
    # Simulate python-telegram-bot not installed (the exact silent-failure trap).
    monkeypatch.setattr(
        ci,
        "missing_dependency",
        lambda channel: "python-telegram-bot" if channel == "telegram" else None,
    )

    with caplog.at_level(logging.WARNING):
        companions = build_social_companions(_DummyMain(), primary="rest")

    assert companions == []  # not started — but loudly, not silently
    assert any(
        "Telegram companion NOT started" in r.message and "python-telegram-bot" in r.message
        for r in caplog.records
    )
