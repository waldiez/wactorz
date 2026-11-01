"""A client for a running Wactorz, over the monitor's REST API and ``/ws``.

Provisional: this surface may still change while its first users are built on it.

    async with Client.from_env() as wz:
        async for part in wz.chat.send("what is running?"):
            print(part.text, end="")

This package imports the standard library and ``aiohttp``, and nothing else of
``wactorz``, so that it can leave for a distribution of its own unchanged. It
never reads the server's configuration: what it needs is passed in.
"""

from .chat import Chat, Part
from .client import Client, Server
from .errors import (
    ClientError,
    EmptyMessageError,
    NoServerError,
    NotFoundError,
    NotOfferedError,
    RefusedError,
    RejectedError,
    TimedOutError,
    TurnInterruptedError,
    UnauthorizedError,
    UnavailableError,
)
from .files import Attachment, Files
from .frames import Disconnected, Dropped, Frame, Reconnected
from .questions import Question, Questions

__all__ = [
    "Attachment",
    "Chat",
    "Client",
    "ClientError",
    "Disconnected",
    "Dropped",
    "EmptyMessageError",
    "Files",
    "Frame",
    "NoServerError",
    "NotFoundError",
    "NotOfferedError",
    "Part",
    "Question",
    "Questions",
    "Reconnected",
    "RefusedError",
    "RejectedError",
    "Server",
    "TimedOutError",
    "TurnInterruptedError",
    "UnauthorizedError",
    "UnavailableError",
]
