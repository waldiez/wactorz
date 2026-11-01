"""A turn of conversation, and the history."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass, field
from typing import Any

from .errors import EmptyMessageError, TurnInterruptedError
from .files import Attachment, Files
from .frames import Disconnected, Event, Frame
from .stream import Listener, Stream
from .transport import Transport

#: The agent a message goes to unless it names another.
MAIN = "main"
#: Seconds to wait for the server to say a message reached an agent. Not how long
#: the turn may take: nothing limits that, as a model may think for a long time.
ACCEPTED_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Part:
    """Something a turn said: a piece of a streamed reply, or a whole one."""

    text: str
    agent: str
    agent_id: str
    #: Whether this is a piece of a reply still arriving, rather than a whole one.
    streaming: bool
    #: The addresses to pick from when the name fits more than one agent. To pick,
    #: send the message again with ``to`` set to one of them.
    choices: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


def _addressed(text: str, to: str | None) -> str:
    """``text`` as it goes out: a mention is what routes a message on the server."""
    if not to or to == MAIN or text.startswith(("/", "@")):
        return text
    return f"@{to} {text}"


def _part_of(frame: Frame) -> Part | None:
    """What ``frame`` said, or None when it said nothing a reader would see."""
    raw = frame.raw
    text = str(raw.get("content") or "")
    if frame.type not in ("chat", "stream_chunk") or not text:
        return None
    return Part(
        text=text,
        agent=str(raw.get("from") or ""),
        agent_id=str(raw.get("agent_id") or ""),
        streaming=frame.type == "stream_chunk",
        choices=tuple(str(choice) for choice in raw.get("choices") or ()),
        raw=raw,
    )


def _belongs_to(event: Event, turn: str) -> bool:
    """Whether ``event`` is of this turn, or is news of the socket it depends on."""
    if not isinstance(event, Frame):
        return True
    if event.type == "turn":
        return event.raw.get("id") == turn
    return event.raw.get("turn_id") == turn


async def _next(listener: Listener, *, accepted: bool) -> Event | None:
    """The next event of a turn, bounded only until the server has accepted it."""
    if accepted:
        return await listener.get()
    try:
        return await asyncio.wait_for(listener.get(), ACCEPTED_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise TurnInterruptedError from exc


class Chat:
    """Talking to agents through one server."""

    def __init__(self, transport: Transport, stream: Stream, files: Files) -> None:
        self._transport = transport
        self._stream = stream
        self._files = files

    async def send(
        self,
        text: str,
        *,
        to: str | None = None,
        attachments: Iterable[str | os.PathLike[str] | Attachment] = (),
    ) -> AsyncGenerator[Part, None]:
        """Say ``text`` and yield what comes back, until the turn ends.

        ``to`` is an agent's name or ``name@node``. Raises `TurnInterruptedError`
        if the socket goes before the turn ends: the message is never sent again,
        as the server may well be working on it.
        """
        if not text.strip():
            raise EmptyMessageError
        files = [await self._files.upload(source) for source in attachments]
        turn = uuid.uuid4().hex
        listener = self._stream.listen(
            Listener(limit=None, accepts=lambda event: _belongs_to(event, turn))
        )
        frame: dict[str, Any] = {
            "type": "chat",
            "id": turn,
            "content": _addressed(text, to),
            "agent_name": to or MAIN,
        }
        if files:
            frame["attachments"] = [file.id for file in files]
        try:
            await self._stream.send(frame, timeout=self._transport.timeout)
            accepted = False
            while True:
                event = await _next(listener, accepted=accepted)
                if event is None or isinstance(event, Disconnected):
                    raise TurnInterruptedError
                if not isinstance(event, Frame):
                    continue
                accepted = True
                if event.type == "turn_end":
                    return
                part = _part_of(event)
                if part is not None:
                    yield part
        finally:
            self._stream.forget(listener)

    async def history(
        self,
        *,
        agent: str | None = None,
        agent_id: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Stored turns, newest first as the server gives them."""
        params: dict[str, Any] = {"limit": limit}
        if agent is not None:
            params["agent"] = agent
        if agent_id is not None:
            params["agent_id"] = agent_id
        if since is not None:
            params["since"] = since
        return await self._transport.request("GET", "/api/chats", params=params)

    async def stop_all(self) -> int:
        """Cancel every turn in progress on the server, anyone's. How many were."""
        answer = await self._transport.request("POST", "/api/chat/stop")
        return int(answer.get("cancelled") or 0)
