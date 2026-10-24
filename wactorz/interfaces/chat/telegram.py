"""Telegram bot interface."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ...core.outbound import Answer
from ...orchestration import SOCIAL
from .outbound import ChannelNotRunningError, NotAUserError, Outbox, outbound
from .social import SocialRateLimiter

if TYPE_CHECKING:
    from ...orchestration import Orchestrator

logger = logging.getLogger(__name__)

#: The most text one Telegram message carries.
MESSAGE_CHARS = 4096

#: How many questions with buttons to keep, for a tap that comes later. A tap
#: on an older message is told the question has expired.
ASKED_KEPT = 1000

#: How much of a message id goes into a button. Telegram allows a short
#: callback payload, so the button names the question by a prefix of its id
#: and the interface keeps the rest.
TOKEN_CHARS = 16


class TelegramInterface:
    """Telegram bot interface.

    With no allow-list the bot runs in **setup mode**: it answers `/start` with
    the sender's user id and nothing else. That keeps the documented way of
    finding your id working (the id is needed to fill the allow-list) without
    letting a stranger reach the LLM or the user's devices.

    Answers through the orchestrator as a social channel: conversation and
    device control, none of the admin surface.

    Also carries messages the other way: an agent's message to a person goes
    out through ``send``, with its options as inline buttons, and a tap on a
    button is published for the agent through the outbox.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        token: str,
        allowed_user_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
        outbox: Outbox | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.token = token
        # allowed_user_id (singular) is the older single-user form; fold it in.
        ids = set(allowed_user_ids or ())
        if allowed_user_id:
            ids.add(int(allowed_user_id))
        self.allowed_user_ids = frozenset(ids)
        self.limiter = SocialRateLimiter()
        self.outbox = outbox or Outbox()
        #: The bot, once `run()` has built it: what `send` talks through.
        self._bot: Any = None
        #: Questions sent with buttons, by the token their buttons carry.
        self._asked: OrderedDict[str, Any] = OrderedDict()

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
        """Send ``text`` to the Telegram user ``user``, with ``options`` as buttons.

        ``user`` is the numeric user id, which is also the id of the private
        chat with the bot. Raises ``RuntimeError`` before the bot runs and
        ``ValueError`` for a user that is not an id.
        """
        bot = self._bot
        if bot is None:
            raise ChannelNotRunningError.telegram()
        try:
            chat_id = int(user)
        except (TypeError, ValueError):
            raise NotAUserError.telegram(user) from None
        message = outbound("telegram", str(chat_id), text, options, message_id, agent_id)
        markup = None
        if message.options:
            from telegram import (  # optional dependency, found by run() already
                InlineKeyboardButton,
                InlineKeyboardMarkup,
            )

            token = message.id[:TOKEN_CHARS]
            self._asked[token] = message
            while len(self._asked) > ASKED_KEPT:
                self._asked.popitem(last=False)
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(option, callback_data=f"{token}:{index}")]
                    for index, option in enumerate(message.options)
                ]
            )
        pieces = [text[i : i + MESSAGE_CHARS] for i in range(0, len(text), MESSAGE_CHARS)] or [""]
        for piece in pieces[:-1]:
            await bot.send_message(chat_id=chat_id, text=piece)
        # The buttons go under the last piece, where the person reads them.
        await bot.send_message(chat_id=chat_id, text=pieces[-1], reply_markup=markup)
        return message.id

    def _answer_for(self, data: str, user: str) -> Answer | None:
        """The answer a tap carrying ``data`` gives, or None for a question not kept."""
        token, _sep, index_text = (data or "").partition(":")
        message = self._asked.get(token)
        if message is None:
            return None
        try:
            index = int(index_text)
        except ValueError:
            return None
        if not 0 <= index < len(message.options):
            return None
        del self._asked[token]
        return Answer(
            id=message.id,
            agent_id=message.agent_id,
            option=message.options[index],
            index=index,
            channel="telegram",
            user=user,
            timestamp=time.time(),
        )

    # ── the bot ───────────────────────────────────────────────────────────────

    async def run(self) -> None:
        try:
            from telegram import Update
            from telegram.constants import ChatAction
            from telegram.ext import (
                Application,
                CallbackQueryHandler,
                CommandHandler,
                ContextTypes,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error(  # noqa: TRY400  # the ImportError is the whole diagnosis
                "python-telegram-bot not installed. Run: pip install python-telegram-bot"
            )
            return

        setup_mode = not self.allowed_user_ids
        if setup_mode:
            logger.warning(
                "[Telegram] TELEGRAM_ALLOWED_USER_IDS is empty — starting in setup mode. "
                "The bot will only reply to /start with your user id; add that id to "
                "TELEGRAM_ALLOWED_USER_IDS (or the add-on option) and restart to enable chat."
            )

        async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            uid = user.id if user else "unknown"
            if not update.message:
                return
            if setup_mode:
                await update.message.reply_text(
                    f"Your Telegram user id is: {uid}\n\n"
                    "This bot isn't configured yet. Add that id to "
                    "TELEGRAM_ALLOWED_USER_IDS (or the add-on's telegram_allowed_user_id "
                    "option) and restart Wactorz to start chatting."
                )
                return
            await update.message.reply_text(
                f"Hi {user.first_name if user else ''}. Telegram interface is online.\n"
                f"Your Telegram user id is: {uid}"
            )

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.message or not update.message.text:
                return

            user = update.effective_user
            if not user:
                return

            logger.info(
                "[Telegram] Message from id=%s username=%s: %s",
                user.id,
                user.username,
                update.message.text[:60],
            )

            if setup_mode:
                await update.message.reply_text(
                    "This bot isn't configured yet. Send /start to get your user id."
                )
                return

            if user.id not in self.allowed_user_ids:
                logger.warning("[Telegram] Rejected message from user %s", user.id)
                return

            sender = str(user.id)
            throttled = self.limiter.check(sender)
            if throttled:
                await update.message.reply_text(throttled)
                return

            text = update.message.text.strip()
            if not update.effective_chat:
                return
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.TYPING
            )

            try:
                response = await self.orchestrator.handle_turn(text, channel=SOCIAL, user=sender)
            finally:
                self.limiter.done(sender)
            response = response or "(no response)"

            for i in range(0, len(response), MESSAGE_CHARS):
                await update.message.reply_text(response[i : i + MESSAGE_CHARS])

        async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """A tap on one of a question's buttons."""
            query = update.callback_query
            if query is None:
                return
            # Every tap is acknowledged, or the person's app shows it as still
            # loading; what the acknowledgement says depends on who tapped.
            user = query.from_user
            if user is None or user.id not in self.allowed_user_ids:
                logger.warning(
                    "[Telegram] Ignored a button tap from user %s", getattr(user, "id", "?")
                )
                await query.answer()
                return
            answer = self._answer_for(str(query.data or ""), str(user.id))
            if answer is None:
                await query.answer(text="That question has expired.")
                return
            await query.answer(text=f"✓ {answer.option}")
            try:
                # The buttons come off: the question is answered, and a second
                # tap would answer it again.
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as exc:
                logger.debug("[Telegram] Could not take the buttons off a message: %s", exc)
            await self.outbox.answered(answer)

        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(CallbackQueryHandler(handle_pick))

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logger.info("[Telegram] Bot starting (polling)...")
        await app.initialize()
        await app.start()
        self._bot = app.bot
        if app.updater:
            await app.updater.start_polling()
        self.outbox.register("telegram", self)
        try:
            await asyncio.Event().wait()
        finally:
            self.outbox.unregister("telegram")
            self._bot = None
