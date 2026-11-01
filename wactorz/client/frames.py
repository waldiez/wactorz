"""What the socket hands a listener: the server's frames, and what happened to the socket.

A frame is kept as the server sent it, so a frame or field this client does not
know passes through rather than breaking it.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Frame:
    """One frame from the server. ``type`` is empty for one that names none."""

    type: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Disconnected:
    """The socket went. Nothing is heard until `Reconnected`, and nothing missed is replayed."""


@dataclass(frozen=True)
class Reconnected:
    """The socket is back. Reread what may have changed: open questions, the history."""


@dataclass(frozen=True)
class Dropped:
    """This listener fell behind and lost its ``count`` oldest events."""

    count: int


Event = Frame | Disconnected | Reconnected | Dropped
