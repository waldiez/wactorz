# SPDX-License-Identifier: Apache-2.0
"""Chat interfaces — connect users to the orchestrator via different channels.

Each channel lives in `interfaces.chat`; this module composes them and keeps
the historical import path working. Supported: CLI (terminal), Discord,
Telegram, WhatsApp (via Twilio) and REST.
"""

import logging
from typing import TYPE_CHECKING, Any

from ..config import CONFIG
from .chat.cli import CLIInterface, resolve_host
from .chat.discord import DiscordInterface
from .chat.outbound import Outbox
from .chat.rest import RESTInterface
from .chat.social import SocialRateLimiter, missing_dependency, social_channel_blocked
from .chat.telegram import TelegramInterface
from .chat.whatsapp import WhatsAppInterface

if TYPE_CHECKING:
    from ..orchestration import Orchestrator

logger: logging.Logger = logging.getLogger(__name__)


#: The interfaces that are chat channels a person is reached on, as the
#: ``--interface`` flag names them. Each registers with the outbox when it runs.
SOCIAL_INTERFACES = frozenset({"discord", "telegram", "whatsapp"})


def build_social_companions(
    orchestrator: "Orchestrator", primary: str, *, outbox: Outbox | None = None
) -> list[Any]:
    """Discord/Telegram interfaces to run alongside the primary interface.

    These talk to the orchestrator as a social channel (no spawn/delete/code), so
    they run next to the dashboard rather than replacing it — how the HA add-on
    exposes them. Skips a channel that's already the primary (no duplicate login),
    one whose library is missing, and one with no sender allow-list — each with a
    log line saying why. Returns interface objects, not coroutines, so the
    selection is easy to unit-test. ``outbox`` is what carries agents' messages
    to them; each channel registers with it as it comes up.
    """
    companions: list[Any] = []
    if CONFIG.discord_token and primary != "discord":
        blocked = social_channel_blocked(
            "discord", CONFIG.discord_token, CONFIG.discord_allowed_user_ids
        )
        if blocked:
            logger.warning("Discord companion NOT started: %s", blocked)
        else:
            companions.append(
                DiscordInterface(
                    orchestrator,
                    token=CONFIG.discord_token,
                    allowed_user_ids=CONFIG.discord_allowed_user_ids,
                    outbox=outbox,
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
                    orchestrator,
                    token=CONFIG.telegram_token,
                    allowed_user_ids=CONFIG.telegram_allowed_user_ids,
                    outbox=outbox,
                )
            )
            logger.info("Telegram companion interface enabled (alongside '%s').", primary)
    return companions


async def run_companion(interface: Any) -> None:
    """Run one companion until it stops, so that its failing stops it alone.

    Companions are gathered with the actor system and the primary interface, and
    an exception out of one would end the gather and the whole server with it. A
    channel that cannot come up (Telegram unreachable, a privilege the bot was
    never granted) is logged and ends here; cancellation still goes through.
    Each channel logs the setting to fix for the failures it can name, such as a
    rejected token, and returns before this is needed.
    """
    try:
        await interface.run()
    except Exception:
        logger.exception(
            "%s stopped; the server and the other interfaces keep running.",
            type(interface).__name__,
        )


def run_all_interfaces(interfaces: list[Any]) -> list[Any]:
    """Turn companion interface objects into guarded ``.run()`` coroutines for gather.

    The outbox goes in the same list: it has a ``run`` of the same shape, and
    runs for as long as the channels it serves. Each is wrapped in
    :func:`run_companion`, so one that fails does not take the others down.
    """
    return [run_companion(iface) for iface in interfaces]


__all__ = [
    "SOCIAL_INTERFACES",
    "CLIInterface",
    "DiscordInterface",
    "Outbox",
    "RESTInterface",
    "SocialRateLimiter",
    "TelegramInterface",
    "WhatsAppInterface",
    "build_social_companions",
    "missing_dependency",
    "resolve_host",
    "run_all_interfaces",
    "run_companion",
    "social_channel_blocked",
]
