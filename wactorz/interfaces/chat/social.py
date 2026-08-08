"""Shared pieces for the social chat channels.

Rate limiting and the checks that decide whether a channel may start at all —
its library is installed, and it has a sender allow-list.
"""

import importlib.util
import logging
import time

from ...config import CONFIG

logger = logging.getLogger(__name__)


_INTERFACE_DEPENDENCIES = {
    "discord": ("discord", "discord.py"),
    "telegram": ("telegram", "python-telegram-bot"),
}


def missing_dependency(channel: str) -> str | None:
    """Return the pip package a social channel needs, or None if it's importable.

    Checked up front so a missing library warns at startup instead of failing
    silently inside run().
    """
    spec = _INTERFACE_DEPENDENCIES.get(channel)
    if not spec:
        return None
    module_name, pip_name = spec
    return None if importlib.util.find_spec(module_name) else pip_name


class SocialRateLimiter:
    """Per-sender request budget for the social channels.

    Every inbound message costs at least one intent classification plus one
    completion, so an unbounded channel is an unbounded bill. Holds a sliding
    one-minute window per sender and refuses one turn at a time — and refuses
    while that sender's previous turn is still running, so a user cannot stack
    concurrent generations by sending faster than the model replies.
    """

    def __init__(self, per_minute: int | None = None) -> None:
        self.per_minute = CONFIG.social_rate_limit_per_min if per_minute is None else per_minute
        self._hits: dict[str, list[float]] = {}
        self._in_flight: set[str] = set()

    def check(self, sender: str) -> str | None:
        """Return None to proceed, or the message to send back instead."""
        if self.per_minute <= 0:  # 0 disables the limit for trusted deployments
            return None
        if sender in self._in_flight:
            return "Still working on your last message — one at a time, please."
        now = time.monotonic()
        window = [t for t in self._hits.get(sender, []) if now - t < 60.0]
        if len(window) >= self.per_minute:
            self._hits[sender] = window
            return (
                f"That's more than {self.per_minute} messages in a minute, so I'm pausing "
                "for a moment. Try again shortly."
            )
        window.append(now)
        self._hits[sender] = window
        self._in_flight.add(sender)
        return None

    def done(self, sender: str) -> None:
        """Release the in-flight slot. Always call this, including on error."""
        self._in_flight.discard(sender)


def social_channel_blocked(channel: str, token: str, allowed: frozenset) -> str | None:
    """Why ``channel`` must not start, or None when it's safe to.

    A bot with a token but no sender allow-list answers anyone who finds it, and
    on these channels answering means spending tokens and controlling the user's
    home. That is not a safe default, so it fails closed with instructions.
    """
    if not token:
        return None
    missing = missing_dependency(channel)
    if missing:
        return (
            f"'{missing}' is not installed. Run `pip install {missing}` "
            "(or `pip install 'wactorz[all]'`) to enable it."
        )
    if not allowed:
        env = {
            "discord": "DISCORD_ALLOWED_USER_IDS",
            "telegram": "TELEGRAM_ALLOWED_USER_IDS",
        }.get(channel, "the allow-list")
        return (
            f"no sender allow-list configured. Set {env} to the user id(s) allowed "
            "to talk to the bot — without it anyone who finds the bot could control "
            "your devices and spend your LLM budget."
        )
    return None
