"""Chat interfaces — connect users to the MainActor via different channels.

Each channel lives in `interfaces.chat`; this module composes them and keeps
the historical import path working. Supported: CLI (terminal), Discord,
Telegram, WhatsApp (via Twilio) and REST.
"""

import logging
from typing import TYPE_CHECKING

from ..config import CONFIG
from .chat.cli import CLIInterface, resolve_host
from .chat.discord import DiscordInterface
from .chat.rest import RESTInterface
from .chat.social import SocialRateLimiter, missing_dependency, social_channel_blocked
from .chat.telegram import TelegramInterface
from .chat.whatsapp import WhatsAppInterface

if TYPE_CHECKING:
    from ..agents.main_actor import MainActor

logger = logging.getLogger(__name__)


def build_social_companions(main_actor: "MainActor", primary: str) -> list:
    """Discord/Telegram interfaces to run alongside the primary interface.

    These talk to the main agent in restricted mode (no spawn/delete/code), so
    they run next to the dashboard rather than replacing it — how the HA add-on
    exposes them. Skips a channel that's already the primary (no duplicate login),
    one whose library is missing, and one with no sender allow-list — each with a
    log line saying why. Returns interface objects, not coroutines, so the
    selection is easy to unit-test.
    """
    companions: list = []
    if CONFIG.discord_token and primary != "discord":
        blocked = social_channel_blocked(
            "discord", CONFIG.discord_token, CONFIG.discord_allowed_user_ids
        )
        if blocked:
            logger.warning("Discord companion NOT started: %s", blocked)
        else:
            companions.append(
                DiscordInterface(
                    main_actor,
                    token=CONFIG.discord_token,
                    allowed_user_ids=CONFIG.discord_allowed_user_ids,
                )
            )
            logger.info("Discord companion interface enabled (alongside '%s').", primary)
    if CONFIG.telegram_token and primary != "telegram":
        blocked = social_channel_blocked(
            "telegram", CONFIG.telegram_token, CONFIG.telegram_allowed_user_ids
        )
        if blocked:
            logger.warning("Telegram companion NOT started: %s", blocked)
        else:
            companions.append(
                TelegramInterface(
                    main_actor,
                    token=CONFIG.telegram_token,
                    allowed_user_ids=CONFIG.telegram_allowed_user_ids,
                )
            )
            logger.info("Telegram companion interface enabled (alongside '%s').", primary)
    return companions


def run_all_interfaces(interfaces: list) -> list:
    """Turn companion interface objects into their ``.run()`` coroutines for gather."""
    return [iface.run() for iface in interfaces]


__all__ = [
    "CLIInterface",
    "DiscordInterface",
    "RESTInterface",
    "SocialRateLimiter",
    "TelegramInterface",
    "WhatsAppInterface",
    "build_social_companions",
    "missing_dependency",
    "resolve_host",
    "run_all_interfaces",
    "social_channel_blocked",
]
