"""How much of main's bridge one node may use.

The bridge answers model calls and history reads for agents on nodes, and the
code on a node is often the model's own, running in the sandbox. Without a
limit, one such agent in a loop spends main's model budget as fast as the
provider answers, and every other node's calls queue behind it. So each kind of
request is a :class:`Lane` with three bounds, each per node:

- **a rate**, a token bucket: a burst, then so many a minute. Over it, the
  request is answered at once with a refusal rather than queued, so the caller
  hears why instead of timing out;
- **how many run at once**, so one node never holds every slot, and another's
  request starts as soon as a slot is free;
- **how many may wait**, so a backlog is refused rather than answered after the
  caller has stopped waiting, at main's expense.

Beside those, the lane bounds how many run at once from all nodes together, so
the provider and the database see a bounded load whatever the number of nodes.
Model calls and history reads are separate lanes: a page of history does not
wait for a slow model call.

The node a request names is checked against its signature before it reaches a
lane (see :mod:`.llm_bridge`), so a node cannot spend another's allowance. With
``WACTORZ_NODE_SIGNING=warn`` an unsigned request may name any node, and so may
take a fresh allowance under an invented name: the limits are per node, and only
signing makes a node's name its own.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

#: How many nodes' buckets are kept. The node in a request is whatever the
#: sender wrote, so without a bound a client inventing names would grow this for
#: as long as main runs. A node forgotten starts again with a full bucket.
NODES_REMEMBERED = 1024


class TokenBucket:
    """``burst`` requests at once, refilled at ``per_minute``.

    ``per_minute`` of 0 or less sets no limit.
    """

    def __init__(
        self, per_minute: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.per_second = per_minute / 60.0
        self.burst = max(1, burst)
        self._clock = clock
        self._tokens = float(self.burst)
        self._at = clock()

    def take(self) -> bool:
        """Spend one request's token, or say there is none left."""
        if self.per_second <= 0:
            return True
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.per_second)
        self._at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


class _Node:
    """One node's share of a lane: its bucket, its slots, and what it has waiting."""

    def __init__(self, bucket: TokenBucket, running: int) -> None:
        self.bucket = bucket
        self.slots = asyncio.Semaphore(running)
        self.admitted = 0


class Lane:
    """One kind of bridge request, with the bounds described in the module docstring."""

    def __init__(
        self,
        *,
        per_minute: float,
        burst: int,
        running_per_node: int,
        waiting_per_node: int,
        running: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.burst = burst
        self.running_per_node = max(1, running_per_node)
        self.waiting_per_node = max(0, waiting_per_node)
        self._clock = clock
        self._slots = asyncio.Semaphore(max(1, running))
        self._nodes: OrderedDict[str, _Node] = OrderedDict()

    def admit(self, node: str) -> str | None:
        """Take a request from ``node`` into the lane, or say why not.

        Admitted, the request holds a place until :meth:`slot` is left. The
        reason a refusal gives is what the bridge tells the node.

        The waiting cap is checked before the bucket because taking a token
        spends it: a request refused as busy would otherwise use up a share of
        the rate it never got. A node over both hears "busy", the one that
        clears first.
        """
        share = self._share(node)
        if share.admitted >= self.running_per_node + self.waiting_per_node:
            return "busy"
        if not share.bucket.take():
            return "rate"
        share.admitted += 1
        return None

    @asynccontextmanager
    async def slot(self, node: str) -> AsyncIterator[None]:
        """Run an admitted request: wait for the node's slot, then the lane's.

        The node's own slot is taken first, so a node with many waiting holds
        only its share of the lane's, and another node's request is not queued
        behind them.
        """
        share = self._share(node)
        try:
            async with share.slots, self._slots:
                yield
        finally:
            share.admitted -= 1

    def _share(self, node: str) -> _Node:
        share = self._nodes.get(node)
        if share is None:
            share = _Node(
                TokenBucket(self.per_minute, self.burst, self._clock), self.running_per_node
            )
            self._nodes[node] = share
            self._forget_idle()
        else:
            self._nodes.move_to_end(node)
        return share

    def _forget_idle(self) -> None:
        """Drop the least recently seen nodes past `NODES_REMEMBERED` that have nothing admitted."""
        excess = len(self._nodes) - NODES_REMEMBERED
        for name in [n for n, share in self._nodes.items() if share.admitted == 0][
            : max(0, excess)
        ]:
            del self._nodes[name]
