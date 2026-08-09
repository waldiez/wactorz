"""Telegram bot interface."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .social import SocialRateLimiter

if TYPE_CHECKING:
    from ...agents.main_actor import MainActor

logger = logging.getLogger(__name__)


class TelegramInterface:
    """Telegram bot interface.

    With no allow-list the bot runs in **setup mode**: it answers `/start` with
    the sender's user id and nothing else. That keeps the documented way of
    finding your id working (the id is needed to fill the allow-list) without
    letting a stranger reach the LLM or the user's devices.
    """

    def __init__(
        self,
        main_actor: "MainActor",
        token: str,
        allowed_user_id: int | None = None,
        allowed_user_ids: frozenset[int] | set[int] | None = None,
    ) -> None:
        self.agent = main_actor
        self.token = token
        # allowed_user_id (singular) is the older single-user form; fold it in.
        ids = set(allowed_user_ids or ())
        if allowed_user_id:
            ids.add(int(allowed_user_id))
        self.allowed_user_ids = frozenset(ids)
        self.limiter = SocialRateLimiter()

    async def run(self) -> None:
        try:
            from telegram import Update
            from telegram.constants import ChatAction
            from telegram.ext import (
                Application,
                CommandHandler,
                ContextTypes,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
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

            # Restricted mode: converse + control devices, no spawn/delete/code.
            try:
                response = await self.agent.process_user_input_restricted(text)
            finally:
                self.limiter.done(sender)
            response = response or "(no response)"

            for i in range(0, len(response), 4096):
                await update.message.reply_text(response[i : i + 4096])

        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logger.info("[Telegram] Bot starting (polling)...")
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling()
        await asyncio.Event().wait()
