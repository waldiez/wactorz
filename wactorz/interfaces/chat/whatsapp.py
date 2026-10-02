"""WhatsApp interface, via Twilio webhooks."""

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ...config import CONFIG, MAX_REQUEST_BYTES
from .social import SocialRateLimiter

if TYPE_CHECKING:
    from ...agents.main import MainActor

logger = logging.getLogger(__name__)

#: How many message ids to remember, to recognise one Twilio delivers again.
SEEN_MESSAGES = 1000


class WhatsAppInterface:
    """WhatsApp via Twilio. Runs an aiohttp webhook server.
    Requires: pip install aiohttp twilio
    Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in environment.
    """

    def __init__(
        self,
        main_actor: "MainActor",
        account_sid: str,
        auth_token: str,
        from_number: str,
        port: int = 8080,
        allowed_numbers: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.agent = main_actor
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.port = port
        # The webhook is a public HTTP endpoint, so anyone who finds it can spend
        # tokens unless senders are pinned. Numbers are compared without the
        # `whatsapp:` prefix Twilio adds.
        self.allowed_numbers = frozenset(
            self._normalize_number(n) for n in (allowed_numbers or CONFIG.whatsapp_allowed_numbers)
        )
        self.limiter = SocialRateLimiter()
        #: Replies still being worked out. The webhook answers Twilio before
        #: the model does, so these are what keeps them alive until they finish.
        self._replies: set[asyncio.Task[None]] = set()
        #: Twilio message ids already taken in, newest last -- see `webhook`.
        self._seen: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _normalize_number(number: str) -> str:
        return str(number or "").strip().replace("whatsapp:", "")

    @staticmethod
    def _signed_url(request: web.Request) -> str:
        """The URL Twilio signed.

        Twilio signs the webhook address configured in its console, which is
        the public one. Behind a reverse proxy the request arrives bearing an
        internal scheme and host, so the forwarded headers win where present.
        """
        proto = request.headers.get("X-Forwarded-Proto")
        host = request.headers.get("X-Forwarded-Host")
        if proto and host:
            return f"{proto}://{host}{request.rel_url}"
        return str(request.url)

    def _signature_valid(self, validator: Any, request: web.Request, params: dict) -> bool:
        """Whether this request carries a signature only Twilio could produce.

        `From` is checked against the allow-list, but a forged POST can name
        any sender, so the signature is what establishes that Twilio sent the
        request at all.
        """
        signature = request.headers.get("X-Twilio-Signature", "")
        if not signature:
            return False
        return bool(validator.validate(self._signed_url(request), params, signature))

    async def _send_message(self, twilio, body: str, to: str) -> None:
        """Send one WhatsApp message, off the event loop.

        The Twilio SDK is synchronous, so calling it directly from the webhook
        handler froze every actor in the process for a full network round-trip —
        on every inbound message.
        """
        await asyncio.to_thread(
            twilio.messages.create,
            body=body,
            from_=f"whatsapp:{self.from_number}",
            to=to,
        )

    def _first_delivery(self, message_sid: str) -> bool:
        """Whether ``message_sid`` is new, remembering it if so. A message with no id is."""
        if not message_sid:
            return True
        if message_sid in self._seen:
            return False
        self._seen[message_sid] = None
        while len(self._seen) > SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return True

    async def _reply(self, twilio: Any, message: str, to: str, sender: str) -> None:
        """Answer one message, after the webhook has already acknowledged it."""
        try:
            # Restricted mode: same guarantees as the other social channels.
            response_text = await self.agent.process_user_input_restricted(message)
            await self._send_message(twilio, response_text, to)
        except Exception:
            logger.exception("[WhatsApp] Could not answer a message from %s", sender)
        finally:
            self.limiter.done(sender)

    def build_app(self) -> web.Application:
        """Assemble the webhook route, without binding a port."""
        from twilio.request_validator import RequestValidator
        from twilio.rest import Client as TwilioClient

        twilio = TwilioClient(self.account_sid, self.auth_token)
        validator = RequestValidator(self.auth_token)

        async def webhook(request):
            data = await request.post()
            if not self._signature_valid(validator, request, dict(data)):
                logger.warning("[WhatsApp] Rejected a request with no valid Twilio signature")
                return web.Response(status=403, text="Forbidden")
            user_msg = data.get("Body", "")
            from_number = data.get("From", "")
            logger.info("[WhatsApp] Message from %s: %s", from_number, user_msg[:60])

            sender = self._normalize_number(from_number)
            if sender not in self.allowed_numbers:
                logger.warning("[WhatsApp] Rejected message from %s (not allow-listed)", sender)
                return web.Response(text="OK")

            # Twilio delivers again when it does not get its answer in time, and
            # a message handled twice is answered twice, and paid for twice.
            if not self._first_delivery(str(data.get("MessageSid", ""))):
                logger.info("[WhatsApp] Ignoring a message from %s delivered again", sender)
                return web.Response(text="OK")

            throttled = self.limiter.check(sender)
            if throttled:
                await self._send_message(twilio, throttled, from_number)
                return web.Response(text="OK")

            # Answered before the model is: a turn outlasts Twilio's wait for
            # the webhook, and a webhook that has not answered is sent again.
            task = asyncio.create_task(self._reply(twilio, user_msg, from_number, sender))
            self._replies.add(task)
            task.add_done_callback(self._replies.discard)
            return web.Response(text="OK")

        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_post("/webhook/whatsapp", webhook)
        return app

    async def run(self) -> None:
        if not (self.auth_token or "").strip():
            logger.error(
                "[WhatsApp] Not starting: TWILIO_AUTH_TOKEN is empty. The webhook checks "
                "Twilio's signature with it, and a signature made with an empty key is one "
                "anybody can make -- set it to the auth token from the Twilio console."
            )
            return
        if not self.allowed_numbers:
            logger.error(
                "[WhatsApp] Not starting: WHATSAPP_ALLOWED_NUMBERS is empty. The webhook is a "
                "public endpoint — set it to the number(s) allowed to message the bot "
                "(e.g. WHATSAPP_ALLOWED_NUMBERS=+306912345678)."
            )
            return

        try:
            app = self.build_app()
        except ImportError:
            logger.error(  # noqa: TRY400, RUF100  # the ImportError is the whole diagnosis
                "Missing deps. Run: pip install twilio"
            )  # the ImportError is the whole diagnosis
            return

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, CONFIG.bind_host, self.port)
        await site.start()
        logger.info("[WhatsApp] Webhook server running on %s:%s", CONFIG.bind_host, self.port)
        await asyncio.Event().wait()  # Run forever
