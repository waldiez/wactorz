"""What an agent says to a person, and what the person answers.

An agent reaches a person through a chat channel: the dashboard, Telegram,
Discord or WhatsApp. It never holds the channel; it publishes one frame on its
own ``agents/<id>/chat`` topic, and the process that runs the channels hands
the frame to the one named in it. A frame may carry *options*, which the
channel shows as buttons where it can and as a numbered list where it cannot.
When the person picks one, the channel publishes the pick on
``agents/<id>/answer``, so the agent that asked, wherever it runs, hears it
on its own topic.

This module is the vocabulary both ends share: the frame and the answer, the
checks on what goes into them, and how a channel without buttons shows a
choice and reads a reply. The channels themselves are in
:mod:`wactorz.interfaces.chat`; the agent's call is :meth:`wactorz.Actor.notify_user`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: The channel a message goes to when none is named: the dashboard shows every
#: ``agents/<id>/chat`` frame, so a message for it needs no delivery of its own.
DASHBOARD = "dashboard"

#: The channels a message may name. The dashboard is one of them: naming it is
#: the same as naming nothing, and lets an agent be explicit.
CHANNELS = frozenset({DASHBOARD, "telegram", "discord", "whatsapp"})

#: How many choices one message may offer. A keyboard of more is not a choice
#: but a menu, and a numbered reply past this is easy to mistype.
MAX_OPTIONS = 10

#: How long one option may be. A button holds a few words; a sentence goes in
#: the message text.
MAX_OPTION_CHARS = 60

#: Where a channel reports the person's pick: the suffix under ``agents/<id>/``.
ANSWER_LEAF = "answer"

#: A reply that is a number, with whatever punctuation a person adds to one.
#: Matched in full against the reply with its ends stripped, so no two
#: repeats sit side by side: a pattern where they did took time in the square
#: of a long run of spaces, which anyone on a chat channel can send.
_NUMBER = re.compile(r"\(?\s*(\d{1,2})\s*[).:]?")


class OptionsError(ValueError):
    """Options a message cannot offer, and why."""

    @classmethod
    def none(cls) -> OptionsError:
        return cls("ask_operator needs options for the person to pick from")

    @classmethod
    def not_a_string(cls, option: Any) -> OptionsError:
        return cls(f"an option must be a string, not {type(option).__name__}")

    @classmethod
    def empty(cls) -> OptionsError:
        return cls("an option cannot be empty")

    @classmethod
    def too_long(cls, text: str) -> OptionsError:
        return cls(
            f"option {text[:20]!r}… is longer than {MAX_OPTION_CHARS} characters; "
            "put the sentence in the message and keep the option to a few words"
        )

    @classmethod
    def twice(cls, text: str) -> OptionsError:
        return cls(f"option {text!r} is offered twice")

    @classmethod
    def too_many(cls, count: int) -> OptionsError:
        return cls(f"{count} options is more than the {MAX_OPTIONS} a message may offer")


class UnknownChannelError(ValueError):
    """A chat channel no interface is called."""

    def __init__(self, channel: str) -> None:
        super().__init__(
            f"no chat channel called {channel!r}; one of {', '.join(sorted(CHANNELS))}"
        )


def check_options(options: Iterable[Any]) -> tuple[str, ...]:
    """``options`` as a tuple of strings, or ``ValueError`` saying what is wrong.

    Each must be a non-empty string within :data:`MAX_OPTION_CHARS`, no two the
    same, and there may be at most :data:`MAX_OPTIONS`. Surrounding whitespace is
    dropped, since a button and a numbered list both show the text bare.
    """
    cleaned: list[str] = []
    for option in options:
        if not isinstance(option, str):
            raise OptionsError.not_a_string(option)
        text = option.strip()
        if not text:
            raise OptionsError.empty()
        if len(text) > MAX_OPTION_CHARS:
            raise OptionsError.too_long(text)
        if text.casefold() in {c.casefold() for c in cleaned}:
            raise OptionsError.twice(text)
        cleaned.append(text)
    if len(cleaned) > MAX_OPTIONS:
        raise OptionsError.too_many(len(cleaned))
    return tuple(cleaned)


def check_channel(channel: str) -> str:
    """``channel`` lower-cased, or ``ValueError`` naming the channels there are."""
    name = (channel or DASHBOARD).strip().lower()
    if name not in CHANNELS:
        raise UnknownChannelError(channel)
    return name


def answer_topic(agent_id: str) -> str:
    """The topic a channel publishes the person's pick on, for the agent that asked."""
    return f"agents/{agent_id}/{ANSWER_LEAF}"


@dataclass(frozen=True)
class OutboundMessage:
    """One message an agent sends to a person, as it travels on ``agents/<id>/chat``.

    ``id`` is what an answer names. ``user`` is who to reach on ``channel``: a
    Telegram or Discord user id, a WhatsApp number; empty for the dashboard,
    which has one operator.
    """

    id: str
    agent_id: str
    sender: str
    text: str
    options: tuple[str, ...] = ()
    channel: str = DASHBOARD
    user: str = ""
    timestamp: float = 0.0

    @classmethod
    def from_chat_frame(cls, agent_id: str, payload: Any) -> OutboundMessage | None:
        """The message in one ``agents/<id>/chat`` frame, or None when the frame is not one.

        A frame is a message to a person when it is addressed to ``user`` and
        says something. The options and the channel are read leniently: a frame
        a channel cannot deliver is better reported by the channel, with the
        frame's own words, than dropped here without a trace.
        """
        if not isinstance(payload, dict):
            return None
        if str(payload.get("to") or "user") != "user":
            return None
        text = str(payload.get("content") or payload.get("text") or "").strip()
        if not text:
            return None
        raw_options = payload.get("options") or ()
        options = (
            tuple(str(o) for o in raw_options) if isinstance(raw_options, list | tuple) else ()
        )
        try:
            timestamp = float(payload.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return cls(
            id=str(payload.get("id") or ""),
            agent_id=agent_id,
            sender=str(payload.get("from") or agent_id),
            text=text,
            options=options,
            channel=str(payload.get("channel") or DASHBOARD).strip().lower(),
            user=str(payload.get("user") or "").strip(),
            timestamp=timestamp,
        )

    @property
    def wants_delivery(self) -> bool:
        """Whether a channel other than the dashboard has to carry this message."""
        return self.channel != DASHBOARD


@runtime_checkable
class TakesDashboardAnswers(Protocol):
    """An agent that takes a pick only from the dashboard, handed over in its process.

    An answer on ``agents/<id>/answer`` is whatever was published there, and
    any agent beside the one that asked can publish there. That is fine for
    an agent's own questions, and no good for one whose answer is a
    permission: such an agent does not listen on the topic, and the web
    server, when the agent runs in its process, hands it the press instead.
    """

    async def dashboard_answer(self, answer: Answer) -> None: ...


@dataclass(frozen=True)
class Answer:
    """A person's pick among a message's options, as it travels on ``agents/<id>/answer``.

    ``option`` is the text picked and ``index`` its place among the options,
    from zero; ``channel`` and ``user`` say where and by whom it was picked.
    """

    id: str
    agent_id: str
    option: str
    index: int
    channel: str
    user: str = ""
    timestamp: float = 0.0

    @property
    def topic(self) -> str:
        """Where this answer is published."""
        return answer_topic(self.agent_id)

    def to_payload(self) -> dict[str, Any]:
        """The frame as it goes on the broker."""
        return {
            "id": self.id,
            "option": self.option,
            "index": self.index,
            "channel": self.channel,
            "user": self.user,
            "from": "user",
            "timestamp": self.timestamp or time.time(),
        }

    @classmethod
    def from_payload(cls, agent_id: str, payload: Any) -> Answer | None:
        """The answer in one ``agents/<id>/answer`` frame, or None when it is not one."""
        if not isinstance(payload, dict):
            return None
        option = str(payload.get("option") or "").strip()
        if not option:
            return None
        try:
            index = int(payload.get("index", 0))
            timestamp = float(payload.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            return None
        return cls(
            id=str(payload.get("id") or ""),
            agent_id=agent_id,
            option=option,
            index=index,
            channel=str(payload.get("channel") or "").strip().lower(),
            user=str(payload.get("user") or "").strip(),
            timestamp=timestamp,
        )


def numbered(text: str, options: Sequence[str]) -> str:
    """``text`` with its options as a numbered list, for a channel with no buttons.

    The person answers with the number or the option's words; see
    :func:`pick_option`. Without options the text goes as it is.
    """
    if not options:
        return text
    lines = [text, ""]
    lines.extend(f"{n}. {option}" for n, option in enumerate(options, start=1))
    lines.append("")
    lines.append("Reply with the number.")
    return "\n".join(lines)


def pick_option(reply: str, options: Sequence[str]) -> int | None:
    """Which of ``options`` a typed ``reply`` picks, from zero, or None if none.

    A number counts from one, as the list showed it; the option's own words
    count too, whatever their case, so "Maintenance" picks "maintenance".
    """
    if not options:
        return None
    match = _NUMBER.fullmatch((reply or "").strip())
    if match:
        n = int(match.group(1))
        return n - 1 if 1 <= n <= len(options) else None
    wanted = (reply or "").strip().casefold()
    if not wanted:
        return None
    for index, option in enumerate(options):
        if option.strip().casefold() == wanted:
            return index
    return None
