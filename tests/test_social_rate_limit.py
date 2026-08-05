"""Per-sender throttling for the social channels.

Every inbound message costs at least an intent classification plus a completion,
so an unthrottled bot is an unbounded LLM bill for whoever owns the API key.
The limiter is the cap: a sliding one-minute window per sender, plus an
in-flight guard so a user can't stack concurrent generations by sending faster
than the model answers.
"""

import wactorz.interfaces.chat_interfaces as ci
from wactorz.interfaces.chat_interfaces import SocialRateLimiter, social_channel_blocked


def test_allows_up_to_the_limit_then_refuses():
    limiter = SocialRateLimiter(per_minute=3)

    for _ in range(3):
        assert limiter.check("user-1") is None
        limiter.done("user-1")

    refusal = limiter.check("user-1")
    assert refusal is not None
    assert "3 messages in a minute" in refusal


def test_limit_is_per_sender():
    limiter = SocialRateLimiter(per_minute=1)

    assert limiter.check("user-1") is None
    limiter.done("user-1")
    assert limiter.check("user-1") is not None
    # A different sender is unaffected by the first one's budget.
    assert limiter.check("user-2") is None


def test_second_message_while_first_is_running_is_refused():
    limiter = SocialRateLimiter(per_minute=10)

    assert limiter.check("user-1") is None  # turn starts, never released
    busy = limiter.check("user-1")

    assert busy is not None
    assert "one at a time" in busy


def test_in_flight_slot_is_released_and_reusable():
    limiter = SocialRateLimiter(per_minute=10)

    assert limiter.check("user-1") is None
    limiter.done("user-1")
    assert limiter.check("user-1") is None


def test_window_expires(monkeypatch):
    limiter = SocialRateLimiter(per_minute=1)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ci.time, "monotonic", lambda: clock["t"])

    assert limiter.check("user-1") is None
    limiter.done("user-1")
    assert limiter.check("user-1") is not None

    clock["t"] += 61.0  # past the one-minute window
    assert limiter.check("user-1") is None


def test_zero_disables_the_limit():
    limiter = SocialRateLimiter(per_minute=0)
    for _ in range(50):
        assert limiter.check("user-1") is None


# ── Startup gate ────────────────────────────────────────────────────────────


def test_no_token_means_nothing_to_block():
    assert social_channel_blocked("discord", "", frozenset()) is None


def test_token_without_allow_list_is_blocked(monkeypatch):
    monkeypatch.setattr(ci, "missing_dependency", lambda _c: None)

    reason = social_channel_blocked("telegram", "t-tok", frozenset())

    assert reason is not None
    assert "TELEGRAM_ALLOWED_USER_IDS" in reason


def test_token_with_allow_list_is_permitted(monkeypatch):
    monkeypatch.setattr(ci, "missing_dependency", lambda _c: None)
    assert social_channel_blocked("telegram", "t-tok", frozenset({42})) is None


def test_missing_library_is_reported_before_the_allow_list(monkeypatch):
    monkeypatch.setattr(ci, "missing_dependency", lambda _c: "python-telegram-bot")

    reason = social_channel_blocked("telegram", "t-tok", frozenset())

    assert "python-telegram-bot" in reason
