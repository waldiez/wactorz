"""Discord bot interface."""

import logging
from typing import TYPE_CHECKING

from ...orchestration import SOCIAL
from .social import SocialRateLimiter

if TYPE_CHECKING:
    from ...orchestration import Orchestrator

logger = logging.getLogger(__name__)


class DiscordInterface:
    """Discord bot interface. Requires: pip install discord.py
    Set DISCORD_BOT_TOKEN in environment.

    Answers through the orchestrator as a social channel: conversation and
    device control, none of the admin surface.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        token: str,
        channel_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.token = token
        self.channel_id = channel_id
        self.allowed_user_ids = frozenset(allowed_user_ids or ())
        self.limiter = SocialRateLimiter()

    async def run(self) -> None:
        try:
            import discord
        except ImportError:
            logger.error(  # noqa: TRY400  # the ImportError is the whole diagnosis
                "discord.py not installed. Run: pip install discord.py"
            )
            return

        # Fail closed: a bot that answers anyone can drain the LLM budget and
        # control the user's home. Refuse to log in without an allow-list.
        if not self.allowed_user_ids:
            logger.error(
                "[Discord] Not starting: DISCORD_ALLOWED_USER_IDS is empty. Set it to the "
                "Discord user id(s) allowed to talk to the bot (enable Developer Mode, "
                "right-click your name, Copy User ID)."
            )
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            logger.info("[Discord] Logged in as %s", client.user)

        @client.event
        async def on_message(message: discord.Message) -> None:
            me = client.user
            if me is None:
                # None until the login handshake finishes; a message cannot be
                # attributed to us before that, so there is nothing to answer.
                return
            if message.author == me:
                return
            if self.channel_id and message.channel.id != self.channel_id:
                return
            if not me.mentioned_in(message):
                return  # Only respond when the bot is mentioned
            if message.author.id not in self.allowed_user_ids:
                logger.warning("[Discord] Rejected message from user %s", message.author.id)
                return

            sender = str(message.author.id)
            throttled = self.limiter.check(sender)
            if throttled:
                await message.channel.send(throttled)
                return

            text = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
            try:
                async with message.channel.typing():
                    response = await self.orchestrator.handle_turn(
                        text, channel=SOCIAL, user=sender
                    )
            finally:
                self.limiter.done(sender)
            for i in range(0, len(response), 2000):
                await message.channel.send(response[i : i + 2000])

        await client.start(self.token)
