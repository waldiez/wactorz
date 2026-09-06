"""A node's publish queue is bounded, and gives way in a stated order.

Unbounded, a broker outage on a node that keeps publishing grows the queue until
the machine runs out of memory -- and these run on Raspberry Pis. Bounded, the
question becomes which message gives way, which is what these pin.
"""

import asyncio
from typing import Any

import pytest

from wactorz import remote_runner


@pytest.fixture(name="runner")
def runner_fixture() -> Any:
    """A runner with only the publish machinery brought up."""
    runner = remote_runner._RemoteRunner.__new__(remote_runner._RemoteRunner)
    runner._pub_queue = asyncio.Queue(maxsize=remote_runner.MAX_QUEUED)
    runner._dropped = 0
    return runner


class TestTheQueueItself:
    def test_the_queue_the_runner_builds_is_bounded(self) -> None:
        # The cap tests below install their own small queue, so without this
        # nothing would notice the production one losing its bound.
        queue = remote_runner._new_pub_queue()

        assert queue.maxsize == remote_runner.MAX_QUEUED
        assert queue.maxsize > 0


class TestClassification:
    @pytest.mark.parametrize(
        "topic",
        [
            "nodes/rpi/heartbeat",
            "agents/abc/logs",
            "agents/abc/metrics",
            "nodes/rpi/status",
        ],
    )
    def test_telemetry_is_droppable(self, topic: str) -> None:
        assert not remote_runner._is_critical(topic)

    @pytest.mark.parametrize(
        "topic",
        [
            "agents/abc/results",
            "agents/abc/errors",
            "agents/abc/manifest",
            "nodes/rpi/migrate_result",
            "nodes/rpi/state_return",
            "nodes/other/spawn",
        ],
    )
    def test_everything_else_is_critical(self, topic: str) -> None:
        # A lost migrate_result or state_return loses an agent; a lost heartbeat
        # is replaced a second later.
        assert remote_runner._is_critical(topic)


class TestTheCap:
    async def test_it_stops_growing(self, runner: Any) -> None:
        runner._pub_queue = asyncio.Queue(maxsize=4)

        for n in range(50):
            await runner.publish("nodes/rpi/heartbeat", {"n": n})

        assert runner._pub_queue.qsize() == 4
        assert runner._dropped > 0

    async def test_telemetry_gives_way_before_control(self, runner: Any) -> None:

        runner._pub_queue = asyncio.Queue(maxsize=3)
        await runner.publish("agents/abc/results", {"keep": "me"})
        for n in range(10):
            await runner.publish("nodes/rpi/heartbeat", {"n": n})

        queued = [runner._pub_queue.get_nowait() for _ in range(runner._pub_queue.qsize())]
        topics = [entry[0] for entry in queued]

        assert "agents/abc/results" in topics, "a result was dropped while telemetry queued"

    async def test_a_full_queue_of_control_drops_the_newcomer(self, runner: Any) -> None:
        # Nothing droppable is queued, so the incoming message gives way rather
        # than the caller being made to wait -- waiting would push a stalled
        # broker back into the agent code that called publish().

        runner._pub_queue = asyncio.Queue(maxsize=2)
        await runner.publish("agents/abc/results", {"first": 1})
        await runner.publish("agents/abc/results", {"second": 2})

        await runner.publish("agents/abc/results", {"third": 3})

        assert runner._pub_queue.qsize() == 2
        assert runner._dropped == 1

    async def test_publishing_never_blocks(self, runner: Any) -> None:
        # `wait_for`, not `asyncio.timeout`: the latter is 3.11+ and this
        # project supports 3.10.
        runner._pub_queue = asyncio.Queue(maxsize=1)

        async def publish_many() -> None:
            for n in range(200):
                await runner.publish("agents/abc/results", {"n": n})

        await asyncio.wait_for(publish_many(), timeout=2)


class TestOrdering:
    async def test_the_rebuild_keeps_the_survivors_in_order(self, runner: Any) -> None:
        # _discard_one_telemetry drains the queue and refills it, because
        # asyncio.Queue offers no way to remove from the middle. A reversal
        # there would silently reorder a node's control messages, so the mix
        # here is chosen to force the rebuild rather than a plain drop: the
        # queue must be full *and* hold something droppable.
        runner._pub_queue = asyncio.Queue(maxsize=3)
        await runner.publish("nodes/rpi/heartbeat", {"tag": "old-telemetry"})
        await runner.publish("agents/abc/results", {"tag": "first-result"})
        await runner.publish("agents/abc/logs", {"tag": "later-telemetry"})

        await runner.publish("agents/abc/errors", {"tag": "arrives-last"})

        queued = [runner._pub_queue.get_nowait() for _ in range(runner._pub_queue.qsize())]

        # Oldest telemetry evicted; everything else keeps its arrival order.
        assert [entry[0] for entry in queued] == [
            "agents/abc/results",
            "agents/abc/logs",
            "agents/abc/errors",
        ]
        assert runner._dropped == 1
