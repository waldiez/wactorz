"""The dashboard's headline totals keep updating while agents are working.

Totals are the one part of a snapshot that queries the database, so a timer
sends them rather than the broker-message path: the cost is one query per
interval, whatever the message rate or agent count. Agent activity arrives over
MQTT, so without the timer a figure that changes while nothing is being
broadcast has no way to reach the browser.

The loop is driven to an outcome here, not for a duration. Counting frames
produced within a fixed number of milliseconds turns any slowdown — coverage
tracing, a loaded machine — into a failure that says the broadcaster stopped
when it had only been slow.
"""

import asyncio
import time
from contextlib import suppress
from typing import Any

import pytest

from wactorz.web import events, runtime, ws


@pytest.fixture(name="sent")
def sent_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what the broadcaster puts on the wire, with a cheap snapshot."""
    frames: list[dict[str, Any]] = []

    async def _capture(msg: dict[str, Any]) -> None:
        frames.append(msg)

    monkeypatch.setattr(ws, "broadcast", _capture)

    def _snapshot(include_totals: bool = True) -> dict[str, Any]:
        # Honours the flag rather than always including totals, so a caller that
        # asks for a totals-free snapshot gets one and the missing key is
        # visible to the assertions below.
        state: dict[str, Any] = {"agents": []}
        if include_totals:
            state["total_cost_usd"] = 1.25
        return state

    monkeypatch.setattr(events, "snapshot", _snapshot)
    monkeypatch.setattr(runtime, "hard_resetting", False)
    return frames


def _interval() -> float:
    """A tick short enough to keep the suite quick, long enough to happen.

    asyncio schedules on the monotonic clock, which Windows resolves to ~15.6ms
    against Linux's ~1ms — and a sleep shorter than that resolution comes back
    almost immediately, so a 10ms interval ran *zero* ticks inside the window
    below rather than a few. Scaling off the clock keeps the Linux timing
    exactly as it was and only stretches it where it has to stretch.
    """
    return max(0.01, time.get_clock_info("monotonic").resolution * 3)


async def _until(
    frames: list[dict[str, Any]],
    count: int,
    interval: float | None = None,
    timeout: float = 5.0,
) -> None:
    """Run the broadcaster until it has sent `count` frames, then stop it.

    Returns as soon as they arrive, and gives up after `timeout` so a
    broadcaster that has genuinely stopped still fails its assertion rather than
    hanging. The ceiling is well under the suite's own timeout, so a real hang
    is still reported as one.
    """
    interval = _interval() if interval is None else interval
    task = asyncio.create_task(ws.totals_broadcaster(interval))
    deadline = time.monotonic() + timeout
    try:
        while len(frames) < count and time.monotonic() < deadline:
            await asyncio.sleep(interval / 2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _tick(interval: float | None = None, times: float = 3.5) -> None:
    """Run the broadcaster for roughly `times` intervals, then stop it.

    For the cases that assert nothing was sent: there is no arrival to wait for,
    so the only option is to run it a while and look. A slow machine makes these
    wait longer than needed, never fail.
    """
    interval = _interval() if interval is None else interval
    task = asyncio.create_task(ws.totals_broadcaster(interval))
    await asyncio.sleep(interval * times)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class TestWithAConnectedDashboard:
    async def test_it_keeps_sending_totals(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "ws_clients", {object()})

        await _until(sent, count=2)

        assert len(sent) >= 2, "totals stopped after the first tick"
        assert all(f["state"]["total_cost_usd"] == 1.25 for f in sent)

    async def test_the_frame_carries_the_totals(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The browser assigns a total only when the key is present, so a frame
        # without it leaves the figure on screen untouched.
        monkeypatch.setattr(runtime, "ws_clients", {object()})

        await _until(sent, count=1)

        assert sent[0]["type"] == "patch"
        assert "total_cost_usd" in sent[0]["state"]


class TestWhenThereIsNothingToTell:
    async def test_no_clients_means_no_snapshot_is_built(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The snapshot is the expensive half; an idle server must not pay for it.
        built = {"n": 0}

        def _counting_snapshot(include_totals: bool = True) -> dict[str, Any]:
            built["n"] += 1
            return {}

        monkeypatch.setattr(events, "snapshot", _counting_snapshot)
        monkeypatch.setattr(runtime, "ws_clients", set())

        await _tick()

        assert sent == []
        assert built["n"] == 0

    async def test_a_hard_reset_is_left_alone(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "ws_clients", {object()})
        monkeypatch.setattr(runtime, "hard_resetting", True)

        await _tick()

        assert sent == []


class TestTheLoopSurvivesFailure:
    async def test_a_failing_tick_does_not_end_it(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Building the snapshot reads the database, so it can fail. If that
        # killed the loop, totals would stop for the life of the process.
        calls = {"n": 0}

        def _flaky(include_totals: bool = True) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database busy")
            return {"total_cost_usd": 2.0}

        monkeypatch.setattr(events, "snapshot", _flaky)
        monkeypatch.setattr(runtime, "ws_clients", {object()})

        await _until(sent, count=1)

        assert calls["n"] >= 2, "the loop stopped after one failure"
        assert sent, "no frame was ever sent after recovering"


class TestTheChatFrameCarriesTotals:
    """The primary path: an agent replies, the cost moves without waiting.

    `heartbeat` and `metrics` both fire on the heartbeat loop, so including
    totals there would put the query back on a path that scales with agent
    count. `chat` follows user activity, which is when anyone is looking.
    """

    @pytest.fixture(name="asked")
    def asked_fixture(self, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        """Record the `include_totals` each broker message asks for."""
        flags: list[bool] = []

        def _snapshot(include_totals: bool = True) -> dict[str, Any]:
            flags.append(include_totals)
            return {}

        monkeypatch.setattr(events, "snapshot", _snapshot)
        monkeypatch.setattr(runtime, "hard_resetting", False)

        async def _noop(msg: dict[str, Any]) -> None:
            return None

        monkeypatch.setattr(ws, "broadcast", _noop)
        return flags

    async def _deliver(self, monkeypatch: pytest.MonkeyPatch, metric: str) -> None:
        from wactorz.web import mqtt

        monkeypatch.setattr(events, "parse_topic", lambda t, p: {"metric": metric})

        async def _noop_mqtt(topic: str, payload: str) -> None:
            return None

        monkeypatch.setattr(mqtt, "broadcast_mqtt_msg", _noop_mqtt)
        await mqtt.handle_message("agents/a1/chat", "{}")

    async def test_a_chat_frame_asks_for_totals(
        self, asked: list[bool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._deliver(monkeypatch, "chat")
        assert asked == [True]

    @pytest.mark.parametrize("metric", ["heartbeat", "metrics", "status", "logs"])
    async def test_the_periodic_frames_do_not(
        self, asked: list[bool], monkeypatch: pytest.MonkeyPatch, metric: str
    ) -> None:
        await self._deliver(monkeypatch, metric)
        assert asked == [False], f"{metric} put the totals query back on the hot path"
