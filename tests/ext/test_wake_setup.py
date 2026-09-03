"""When the wake loop runs, and what it does with what it hears.

The loop owns the microphone, so whether it is running is not a detail: it
decides who holds the device. These drive the seam that starts and stops it,
against a loop double -- what a real one hears belongs to a room.
"""

import asyncio
from typing import Any

import pytest
from aiohttp import web

from wactorz import config
from wactorz.core import voice_settings
from wactorz.ext import stt, wake
from wactorz.ext.stt import listener


class _Loop:
    """Stands in for the loop that owns the microphone."""

    def __init__(self, _model_dir: object, _phrases: object, on_turn: Any) -> None:
        self.on_turn = on_turn
        self.stopped = False
        self.ran = False

    async def run(self) -> None:
        self.ran = True
        while not self.stopped:
            await asyncio.sleep(0.01)

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(name="built")
def built_fixture(monkeypatch: pytest.MonkeyPatch) -> list[_Loop]:
    """Record every loop the seam builds, without building a real one."""
    made: list[_Loop] = []

    def _build(*args: object, **kwargs: object) -> _Loop:
        made.append(_Loop(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(wake.loop, "WakeLoop", _build)
    monkeypatch.setattr(wake, "_running", None)
    monkeypatch.setattr(wake, "_task", None)
    monkeypatch.setattr(listener, "_owner", None)
    # A model this deployment could load: the seam refuses to start without one,
    # and that refusal has a test of its own.
    monkeypatch.setattr(config, "WAKE_MODEL_DIR", "/models/kws")
    return made


def _configured(monkeypatch: pytest.MonkeyPatch, *, listening: str, waking: str) -> None:
    monkeypatch.setattr(voice_settings, "listening", lambda: listening)
    monkeypatch.setattr(voice_settings, "waking", lambda: waking)


class TestWhetherItRunsAtAll:
    async def test_it_waits_for_a_phrase_when_asked_to(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        _configured(monkeypatch, listening="host", waking="on")

        await wake.reconcile()
        try:
            assert len(built) == 1
            # And it holds the device, which is how a turn on demand knows to ask.
            assert listener._owner is built[0]
        finally:
            await wake.shutdown()

    async def test_it_stays_out_of_the_way_when_not_asked_to(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        _configured(monkeypatch, listening="host", waking="off")

        await wake.reconcile()

        assert not built
        assert listener._owner is None

    async def test_it_does_not_run_on_a_branch_with_no_microphone(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        # Every other branch records in a browser or not at all, so there is no
        # device here for a loop to own and nothing for a phrase to interrupt.
        _configured(monkeypatch, listening="browser", waking="on")

        await wake.reconcile()

        assert not built


class TestChangingItWhileRunning:
    async def test_turning_it_off_stops_the_loop_and_frees_the_device(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        _configured(monkeypatch, listening="host", waking="on")
        await wake.reconcile()

        _configured(monkeypatch, listening="host", waking="off")
        await wake.reconcile()

        assert built[0].stopped
        assert listener._owner is None

    async def test_leaving_it_on_does_not_start_a_second_one(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        # Two loops would be two claims on one device, and the second would
        # spend its life failing to open what the first is holding.
        _configured(monkeypatch, listening="host", waking="on")

        await wake.reconcile()
        await wake.reconcile()
        try:
            assert len(built) == 1
        finally:
            await wake.shutdown()

    async def test_leaving_the_branch_stops_it_too(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        _configured(monkeypatch, listening="host", waking="on")
        await wake.reconcile()

        _configured(monkeypatch, listening="server", waking="on")
        await wake.reconcile()

        assert built[0].stopped


class TestHandingATurnBackToTheEventLoop:
    """The seam between the two halves: audio captured on a thread, routed on the loop.

    Everything either side of this was tested and worked, and the turn was still
    lost here -- so it is driven from a real thread rather than called directly.
    """

    async def test_a_turn_captured_on_a_thread_reaches_the_router(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        routed: list[tuple[bytes, str]] = []

        async def _route(clip: bytes, source: str) -> None:
            routed.append((clip, source))

        monkeypatch.setattr(stt, "route_heard_clip", _route)
        _configured(monkeypatch, listening="host", waking="on")
        await wake.reconcile()
        try:
            handed = built[0].on_turn
            # From a thread with no loop of its own, which is what the capture
            # thread is: asking that thread for "the" event loop raises.
            await asyncio.to_thread(handed, b"a clip")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert routed == [(b"a clip", "wake")]
        finally:
            await wake.shutdown()

    async def test_a_turn_in_flight_is_held_until_it_finishes(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        # The event loop keeps only a weak reference, so a task nothing else
        # holds can be collected before it runs -- losing the turn silently.
        started = asyncio.Event()
        release = asyncio.Event()

        async def _route(_clip: bytes, source: str = "") -> None:
            assert source == "wake"
            started.set()
            await release.wait()

        monkeypatch.setattr(stt, "route_heard_clip", _route)
        _configured(monkeypatch, listening="host", waking="on")
        await wake.reconcile()
        try:
            await asyncio.to_thread(built[0].on_turn, b"a clip")
            await asyncio.wait_for(started.wait(), timeout=1)

            assert len(wake._routing) == 1

            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            # And let go once it is done, so the set does not grow for ever.
            assert not wake._routing
        finally:
            await wake.shutdown()


class TestWithoutAModel:
    async def test_it_says_so_rather_than_starting(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        # Weights are not shipped with the code, so "on" without a model is a
        # deployment half-configured -- and a loop that started would fail on
        # every retry instead of saying what is missing.
        _configured(monkeypatch, listening="host", waking="on")
        monkeypatch.setattr(config, "WAKE_MODEL_DIR", "")
        monkeypatch.setattr(wake, "_said_no_model", False)
        said: list[str] = []
        monkeypatch.setattr(wake.logger, "warning", lambda msg, *_a: said.append(msg))

        await wake.reconcile()
        await wake.reconcile()

        assert not built
        assert listener._owner is None
        # Said once: the answer does not change between two settings saves.
        assert len(said) == 1


class TestShuttingDown:
    async def test_the_loop_is_stopped_with_the_app(
        self, monkeypatch: pytest.MonkeyPatch, built: list[_Loop]
    ) -> None:
        # A loop left running holds the microphone after the process that owns
        # it has gone, which nothing else can take back.
        _configured(monkeypatch, listening="host", waking="on")
        app = web.Application()
        wake.setup(app)
        await wake.reconcile()

        for handler in app.on_cleanup:
            await handler(app)

        assert built[0].stopped
        assert listener._owner is None

    async def test_shutting_down_when_nothing_ran_is_quiet(self, built: list[_Loop]) -> None:
        await wake.shutdown()

        assert not built
