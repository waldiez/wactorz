"""How long a person waits in the chat.

A turn starts when a message reaches the server and ends when the reply is
complete. Two waits are recorded: until the first words of the reply arrive,
which is what a person notices while an answer streams in, and until the turn
is over. Both are labelled by where the message went, a bounded set, rather
than by agent, whose names are open-ended.

A turn the person stops is not recorded: how long it ran says nothing about how
long answers take.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, the way the other module-level metrics are.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import Histogram

#: Where a turn went: a slash command, the orchestrator (a message that names
#: no agent), an agent in this process named with ``@``, an agent on a node, or
#: a name nothing answers to.
COMMAND = "command"
ORCHESTRATOR = "orchestrator"
LOCAL = "local"
REMOTE = "remote"
UNROUTED = "unrouted"

#: Upper bounds in seconds, from a command answered at once to a long
#: orchestrated turn: planning, spawning and several model calls, which can run
#: past five minutes.
_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)

FIRST_REPLY = Histogram(
    "wactorz_chat_first_reply_seconds",
    "Time from a chat message reaching the server to the first words of its reply.",
    labelnames=("kind",),
    buckets=_BUCKETS,
    registry=None,
)
TURN_DURATION = Histogram(
    "wactorz_chat_turn_duration_seconds",
    "Time from a chat message reaching the server to its reply being complete.",
    labelnames=("kind",),
    buckets=_BUCKETS,
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (FIRST_REPLY, TURN_DURATION)

Send = Callable[..., Awaitable[Any]]


class TurnTimer:
    """Times one chat turn, from its creation to `finish`."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._started = time.monotonic()
        self._first_reply: float | None = None

    def watch(self, send: Send) -> Send:
        """``send``, noting when it is first called: the first words of the reply."""

        async def watched(*args: Any, **kwargs: Any) -> Any:
            if self._first_reply is None:
                self._first_reply = time.monotonic() - self._started
            return await send(*args, **kwargs)

        return watched

    def finish(self) -> None:
        """Record the turn as over now. A turn that said nothing records no first reply."""
        if self._first_reply is not None:
            FIRST_REPLY.labels(kind=self.kind).observe(self._first_reply)
        TURN_DURATION.labels(kind=self.kind).observe(time.monotonic() - self._started)
