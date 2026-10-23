"""Discord bot interface."""

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ...core.outbound import numbered
from ...orchestration import SOCIAL
from .outbound import Outbox, PendingChoices, outbound
from .social import SocialRateLimiter

if TYPE_CHECKING:
    from ...orchestration import Orchestrator

logger = logging.getLogger(__name__)

#: The most text one Discord message carries.
MESSAGE_CHARS = 2000


class DiscordInterface:
    """Discord bot interface. Requires: pip install discord.py
    Set DISCORD_BOT_TOKEN in environment.

    Answers through the orchestrator as a social channel: conversation and
    device control, none of the admin surface.

    Also carries messages the other way: an agent's message to a person goes
    out through ``send`` as a direct message, its options numbered, and a reply
    with the number is published for the agent through the outbox.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        token: str,
        channel_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
        outbox: Outbox | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.token = token
        self.channel_id = channel_id
        self.allowed_user_ids = frozenset(allowed_user_ids or ())
        self.limiter = SocialRateLimiter()
        self.outbox = outbox or Outbox()
        self.choices = PendingChoices()
        #: The client, once `run()` has built it: what `send` talks through.
        self._client: Any = None

    # ── outbound ──────────────────────────────────────────────────────────────

    async def send(
        self,
        user: str,
        text: str,
        options: Sequence[str] = (),
        *,
        message_id: str = "",
        agent_id: str = "",
    ) -> str:
        """Send ``text`` to the Discord user ``user`` as a direct message.

        Options go as a numbered list, which the person answers with the number
        or the option's words. ``user`` is the numeric user id. Raises
        ``RuntimeError`` before the bot runs and ``ValueError`` for a user that
        is not an id.
        """
        client = self._client
        if client is None:
            raise RuntimeError("the Discord bot is not running")
        try:
            user_id = int(user)
        except (TypeError, ValueError):
            raise ValueError(f"a Discord user is a numeric id, not {user!r}") from None
        message = outbound("discord", str(user_id), text, options, message_id, agent_id)
        recipient = client.get_user(user_id) or await client.fetch_user(user_id)
        body = numbered(message.text, message.options)
        for i in range(0, len(body), MESSAGE_CHARS):
            await recipient.send(body[i : i + MESSAGE_CHARS])
        self.choices.remember(message)
        return message.id

    # ── the bot ───────────────────────────────────────────────────────────────

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
            text = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
            sender = str(message.author.id)
            if message.author.id in self.allowed_user_ids:
                # A reply to a question the bot asked: "2", or the option's
                # words. Not a turn for the model, and no mention needed, since
                # the bot wrote first.
                answer = self.choices.match(sender, text)
                if answer is not None:
                    await self.outbox.answered(answer)
                    return
            if not me.mentioned_in(message):
                return  # Only respond when the bot is mentioned
            if message.author.id not in self.allowed_user_ids:
                logger.warning("[Discord] Rejected message from user %s", message.author.id)
                return

            throttled = self.limiter.check(sender)
            if throttled:
                await message.channel.send(throttled)
                return

            try:
                async with message.channel.typing():
                    response = await self.orchestrator.handle_turn(
                        text, channel=SOCIAL, user=sender
                    )
            finally:
                self.limiter.done(sender)
            for i in range(0, len(response), MESSAGE_CHARS):
                await message.channel.send(response[i : i + MESSAGE_CHARS])

        self._client = client
        self.outbox.register("discord", self)
        try:
            await client.start(self.token)
        finally:
            self.outbox.unregister("discord")
            self._client = None
