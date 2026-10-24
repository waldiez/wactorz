"""The outbox: where a message from an agent meets the channel that reaches the person.

An agent, on main or on a node, publishes what it has to say on its own
``agents/<id>/chat`` topic (:meth:`wactorz.Actor.notify_user`). The chat
channels run in main's process and have no mailbox of their own, so the
:class:`Outbox` listens on the broker for those frames and hands each one
addressed to a channel to that channel's ``send``. A channel registers with
its name when it is up, and is forgotten when it is not, so a frame for a
channel that is not running is reported, with its words, rather than lost.

The answer travels the other way on the same link: a channel that has the
person's pick calls :meth:`Outbox.answered`, and the pick goes out on
``agents/<id>/answer`` for the agent that asked.

A channel without buttons shows the options numbered and reads the reply with
:class:`PendingChoices`, which remembers the latest choice offered to each
person so a bare "2" can be matched to it.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    reconnect_wait,
    session_kwargs,
)
from ...core.outbound import Answer, OutboundMessage, check_options, pick_option

logger = logging.getLogger(__name__)

#: Every agent's chat frames, whichever machine the agent is on.
CHAT_FRAMES = "agents/+/chat"

#: How long the outbox waits before dialling the broker again after losing it.
RECONNECT_SECONDS = 5.0


class ChannelNotRunningError(RuntimeError):
    """A message handed to a channel before its bot, or webhook, is up."""

    @classmethod
    def discord(cls) -> "ChannelNotRunningError":
        return cls("the Discord bot is not running")

    @classmethod
    def telegram(cls) -> "ChannelNotRunningError":
        return cls("the Telegram bot is not running")

    @classmethod
    def whatsapp(cls) -> "ChannelNotRunningError":
        return cls("the WhatsApp webhook is not running")


class NotAUserError(ValueError):
    """A ``user`` that is not how the channel names a person."""

    @classmethod
    def discord(cls, user: Any) -> "NotAUserError":
        return cls(f"a Discord user is a numeric id, not {user!r}")

    @classmethod
    def telegram(cls, user: Any) -> "NotAUserError":
        return cls(f"a Telegram user is a numeric id, not {user!r}")

    @classmethod
    def whatsapp(cls) -> "NotAUserError":
        return cls("a WhatsApp user is a phone number")


class Sender(Protocol):
    """What a chat channel offers the outbox: one way to reach one person.

    ``message_id`` and ``agent_id`` are what the person's answer names, when
    the message offers options; a call from host code may leave them out, and
    an answer to such a message is logged rather than published.
    """

    async def send(
        self,
        user: str,
        text: str,
        options: Sequence[str] = (),
        *,
        message_id: str = "",
        agent_id: str = "",
    ) -> str:
        """Deliver ``text`` to ``user``, offering ``options``; return the message's id."""
        ...


def outbound(
    channel: str,
    user: str,
    text: str,
    options: Sequence[str],
    message_id: str,
    agent_id: str,
) -> OutboundMessage:
    """The message a channel's ``send`` is about to deliver, with an id if it had none."""
    return OutboundMessage(
        id=message_id or uuid.uuid4().hex,
        agent_id=agent_id,
        sender=agent_id or channel,
        text=text,
        options=check_options(options),
        channel=channel,
        user=user,
        timestamp=time.time(),
    )


class PendingChoices:
    """The latest choice offered to each person on one channel, until they answer.

    For a channel that shows options as a numbered list: the reply is a bare
    number or the option's words, and only the message it answers gives them
    meaning. One per person, the newest: a person asked twice answers the later
    question, and the earlier one stays for the dashboard. Kept in memory, so a
    reply to a choice offered before a restart reads as an ordinary message.
    """

    def __init__(self) -> None:
        self._latest: dict[str, OutboundMessage] = {}

    def remember(self, message: OutboundMessage) -> None:
        """Keep ``message`` as the choice ``message.user`` has to make, if it offers one."""
        if message.options and message.user:
            self._latest[message.user] = message

    def match(self, user: str, reply: str) -> Answer | None:
        """The answer ``reply`` gives to what ``user`` was last asked, or None.

        A match takes the choice off the table, so the next message from that
        person is a message again.
        """
        message = self._latest.get(user)
        if message is None:
            return None
        index = pick_option(reply, message.options)
        if index is None:
            return None
        del self._latest[user]
        return Answer(
            id=message.id,
            agent_id=message.agent_id,
            option=message.options[index],
            index=index,
            channel=message.channel,
            user=user,
            timestamp=time.time(),
        )

    def __len__(self) -> int:
        return len(self._latest)


class Outbox:
    """The channels that are running, and the broker link that feeds them.

    ``register`` and ``unregister`` are the channels' to call as they come up
    and go down. ``run`` keeps the link to the broker for as long as the process
    runs, as the other interfaces' ``run`` do; ``handle_frame`` is the part
    with rules, kept apart from the loop so it can be exercised without a broker.
    """

    def __init__(self, broker: str = "localhost", port: int = 1883) -> None:
        self._broker = broker
        self._port = port
        self._senders: dict[str, Sender] = {}
        self._client: Any = None

    # ── channels ──────────────────────────────────────────────────────────────

    def register(self, name: str, sender: Sender) -> None:
        """Make ``sender`` the way to reach people on the channel called ``name``."""
        self._senders[name.lower()] = sender
        logger.info("[outbox] %s can now deliver messages from agents", name)

    def unregister(self, name: str) -> None:
        """Forget the channel called ``name``; messages for it are reported, not sent."""
        if self._senders.pop(name.lower(), None) is not None:
            logger.info("[outbox] %s no longer delivers messages from agents", name)

    def channels(self) -> frozenset[str]:
        """The channels a message can be delivered on right now."""
        return frozenset(self._senders)

    # ── delivery ──────────────────────────────────────────────────────────────

    async def deliver(self, message: OutboundMessage) -> bool:
        """Hand ``message`` to its channel. False, with a log line, when it cannot be.

        The dashboard needs no delivery: it shows the frame itself. A channel
        that is not running, a message with nobody to reach, and a send that
        fails are each logged with the message's words, so what an agent tried
        to say is on the console even when nobody was reached.
        """
        if not message.wants_delivery:
            return False
        sender = self._senders.get(message.channel)
        if sender is None:
            running = ", ".join(sorted(self._senders)) or "none"
            logger.warning(
                "[outbox] %s asked to reach %r on %s, which is not running (running: %s): %r",
                message.sender,
                message.user,
                message.channel,
                running,
                message.text[:120],
            )
            return False
        if not message.user:
            logger.warning(
                "[outbox] %s sent a message on %s without saying who to reach: %r",
                message.sender,
                message.channel,
                message.text[:120],
            )
            return False
        try:
            await sender.send(
                message.user,
                message.text,
                message.options,
                message_id=message.id,
                agent_id=message.agent_id,
            )
        except Exception:
            logger.exception(
                "[outbox] %s could not reach %r on %s with: %r",
                message.sender,
                message.user,
                message.channel,
                message.text[:120],
            )
            return False
        return True

    async def handle_frame(self, topic: str, payload: str) -> bool:
        """Deliver the message in one broker frame, if it is one. True when delivered."""
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "agents" or parts[2] != "chat":
            return False
        try:
            data = json.loads(payload)
        except ValueError:
            return False
        message = OutboundMessage.from_chat_frame(parts[1], data)
        if message is None:
            return False
        return await self.deliver(message)

    # ── answers ───────────────────────────────────────────────────────────────

    async def answered(self, answer: Answer) -> bool:
        """Publish ``answer`` for the agent that asked. False when the broker is away.

        At-least-once: the person pressed a button, and a pick that reaches no
        one is a question asked again.
        """
        if not answer.agent_id:
            logger.info(
                "[outbox] %r answered %r to a message no agent sent; nothing to tell",
                answer.user,
                answer.option,
            )
            return False
        client = self._client
        if client is None:
            logger.warning(
                "[outbox] no broker link to carry %r's answer %r to %s",
                answer.user,
                answer.option,
                answer.agent_id[:8],
            )
            return False
        try:
            await client.publish(answer.topic, json.dumps(answer.to_payload()), qos=1)
        except Exception:
            logger.exception("[outbox] could not publish an answer for %s", answer.agent_id[:8])
            return False
        return True

    # ── the broker link ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Keep the link to the broker, delivering chat frames, until cancelled."""
        logger.info("[outbox] Connecting to MQTT %s:%s...", self._broker, self._port)
        while True:
            try:
                async with mqtt_client(
                    self._broker,
                    self._port,
                    identifier=client_id("outbox", install_id()),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    self._client = client
                    await client.subscribe(CHAT_FRAMES, qos=1)
                    logger.info("[outbox] MQTT connected.")
                    async for message in client.messages:
                        await self.handle_frame(
                            str(message.topic), message.payload.decode(errors="replace")
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[outbox] MQTT error: %s. Reconnecting in about %.0fs...",
                    exc,
                    RECONNECT_SECONDS,
                )
                await asyncio.sleep(reconnect_wait(RECONNECT_SECONDS))
            finally:
                self._client = None
