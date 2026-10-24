"""The bounds on how much of main's bridge one node may use."""

import asyncio

import pytest

from tests.waiting import until
from wactorz.agents.main import bridge_limits
from wactorz.agents.main.bridge_limits import Lane, TokenBucket


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def lane(clock: _Clock | None = None, **over: int) -> Lane:
    settings = {
        "per_minute": 0,
        "burst": 0,
        "running_per_node": 1,
        "waiting_per_node": 1,
        "running": 4,
        **over,
    }
    return Lane(**settings, clock=clock or _Clock())


class TestTheBucket:
    def test_a_burst_then_so_many_a_minute(self) -> None:
        clock = _Clock()
        bucket = TokenBucket(per_minute=6, burst=2, clock=clock)

        assert [bucket.take() for _ in range(3)] == [True, True, False]
        clock.now += 10
        assert [bucket.take() for _ in range(2)] == [True, False]

    def test_it_refills_no_further_than_the_burst(self) -> None:
        clock = _Clock()
        bucket = TokenBucket(per_minute=60, burst=2, clock=clock)
        bucket.take()

        clock.now += 3600

        assert [bucket.take() for _ in range(3)] == [True, True, False]

    def test_zero_sets_no_limit(self) -> None:
        bucket = TokenBucket(per_minute=0, burst=0, clock=_Clock())

        assert all(bucket.take() for _ in range(1000))


class TestTheLane:
    def test_a_node_may_have_so_many_admitted_and_no_more(self) -> None:
        lanes = lane(running_per_node=2, waiting_per_node=1)

        assert [lanes.admit("a") for _ in range(4)] == [None, None, None, "busy"]
        assert lanes.admit("b") is None, "another node has its own share"

    def test_past_the_rate_it_says_so(self) -> None:
        lanes = lane(per_minute=1, burst=1)

        assert lanes.admit("a") is None
        assert lanes.admit("b") is None
        assert lanes.admit("a") == "rate"

    async def test_a_busy_refusal_spends_none_of_the_rate(self) -> None:
        lanes = lane(per_minute=1, burst=2, running_per_node=1, waiting_per_node=0)
        assert lanes.admit("a") is None

        async with lanes.slot("a"):
            assert [lanes.admit("a") for _ in range(3)] == ["busy"] * 3

        assert lanes.admit("a") is None, "the second token is still there"

    async def test_leaving_a_slot_makes_room_for_another(self) -> None:
        lanes = lane(running_per_node=1, waiting_per_node=0)
        assert lanes.admit("a") is None

        async with lanes.slot("a"):
            assert lanes.admit("a") == "busy"

        assert lanes.admit("a") is None

    async def test_a_node_waits_for_its_own_slot_not_the_lane_s(self) -> None:
        lanes = lane(running_per_node=1, waiting_per_node=1, running=2)
        entered: list[str] = []
        leave = asyncio.Event()

        async def run(node: str) -> None:
            async with lanes.slot(node):
                entered.append(node)
                await leave.wait()

        for node in ("a", "a", "b"):
            assert lanes.admit(node) is None
        tasks = [asyncio.create_task(run(node)) for node in ("a", "a", "b")]
        await until(lambda: len(entered) == 2, "a's first and b to enter")

        assert entered == ["a", "b"], "a's second waits for a's first; b does not"
        leave.set()
        await asyncio.gather(*tasks)
        assert entered == ["a", "b", "a"]

    def test_idle_nodes_are_forgotten_past_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bridge_limits, "NODES_REMEMBERED", 2)
        lanes = lane(per_minute=1, burst=1)
        lanes.admit("busy")  # holds its place: never forgotten while admitted
        lanes._share("idle")  # seen, nothing admitted

        for node in ("x", "y", "z"):
            lanes._share(node)

        assert "busy" in lanes._nodes
        assert "idle" not in lanes._nodes
        assert lanes.admit("busy") == "rate", "a node still remembered keeps its bucket"
