"""Cancelling a task you own must not absorb a cancellation aimed at you.

Three shutdown helpers cancel a task they started and then wait for it. Catching
``CancelledError`` from that wait is right for the task's *own* cancellation and
wrong for the caller's — the two are indistinguishable at the ``except``. When
``Actor.stop`` got this wrong, the supervisor's watch loop consumed its own
cancellation, resumed its poll loop, and left ``Supervisor.stop()`` waiting on a
task that could never finish.

``asyncio.gather(task, return_exceptions=True)`` draws the distinction: the
inner task's ``CancelledError`` comes back as a value, while one aimed at the
awaiting task still propagates.

**What is not tested here, and why.** That the *caller's* cancellation now
survives is the point of the change, and it has no test: it needs the caller to
be cancelled while blocked on the inner task, and the inner task finishes
cancelling in the same loop turn, so the window cannot be hit deterministically.
An attempt at one passed against the old swallowing code as well, which is worse
than no test. What is pinned below is the surrounding behaviour — the task is
stopped, the reference cleared, and calling twice is harmless — plus the asyncio
property the change relies on.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import Actor, Message
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.registry import ActorRegistry, Supervisor
from wactorz.web import ws


class _SilentSocket:
    """A socket that accepts everything, so the writer never retires itself."""

    async def send_str(self, _payload: str) -> None:
        return None

    async def close(self) -> None:
        return None


class TestChannelClose:
    async def test_the_writer_is_stopped(self) -> None:
        channel = ws.Channel(_SilentSocket())  # type: ignore[arg-type]

        await channel.close()

        assert channel._writer.done()

    async def test_closing_twice_is_harmless(self) -> None:
        channel = ws.Channel(_SilentSocket())  # type: ignore[arg-type]

        await channel.close()
        await channel.close()


class TestPublisherDisconnect:
    async def test_the_drain_loop_is_stopped(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._task = asyncio.create_task(asyncio.Event().wait())

        await pub.disconnect()

        assert pub._task.done()

    async def test_disconnecting_without_a_task_is_harmless(self, tmp_path: Path) -> None:
        await MQTTPublisher(db_path=str(tmp_path / "outbox.db")).disconnect()


class TestSupervisorStop:
    @staticmethod
    def _supervisor() -> Supervisor:
        return Supervisor(ActorRegistry(), lambda _actor: None, poll_interval=0.01)

    async def test_the_watch_task_is_stopped_and_forgotten(self) -> None:
        supervisor = self._supervisor()
        await supervisor.start()
        watch = supervisor._watch_task

        await supervisor.stop()

        assert watch is not None and watch.done()
        # Cleared as well as stopped, so a later start() cannot find a dead one.
        assert supervisor._watch_task is None

    async def test_stopping_without_starting_is_harmless(self) -> None:
        await self._supervisor().stop()


async def _dies_with(error: Exception) -> None:
    """A loop that crashes rather than being cancelled."""
    raise error


class TestACrashedLoopIsReported:
    """`gather` retrieves the exception, so nothing else will report it.

    A bare `await task` re-raised a real error into the caller, and anything not
    retrieved got asyncio's "never retrieved" warning. `return_exceptions=True`
    ends both, so each site has to say something itself or the crash disappears
    at exactly the moment someone is asking why the thing stopped working.
    """

    async def test_the_channel_writer_crash_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = ws.Channel(_SilentSocket())  # type: ignore[arg-type]
        channel._writer.cancel()
        await asyncio.gather(channel._writer, return_exceptions=True)
        channel._writer = asyncio.create_task(_dies_with(RuntimeError("writer blew up")))
        await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING, logger="wactorz.web.ws"):
            await channel.close()

        assert "writer blew up" in caplog.text

    async def test_the_publisher_drain_crash_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._task = asyncio.create_task(_dies_with(RuntimeError("drain blew up")))
        await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING, logger="wactorz.core.mqtt_publisher"):
            await pub.disconnect()

        assert "drain blew up" in caplog.text

    async def test_the_watch_loop_crash_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        supervisor = Supervisor(ActorRegistry(), lambda _actor: None, poll_interval=0.01)
        supervisor._watch_task = asyncio.create_task(_dies_with(RuntimeError("watch blew up")))
        await asyncio.sleep(0)

        with caplog.at_level(logging.ERROR, logger="wactorz.core.registry"):
            await supervisor.stop()

        assert "watch blew up" in caplog.text

    async def test_an_ordinary_cancellation_is_not_reported_as_a_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._task = asyncio.create_task(asyncio.Event().wait())

        with caplog.at_level(logging.WARNING, logger="wactorz.core.mqtt_publisher"):
            await pub.disconnect()

        # Every shutdown cancels this task. Reporting that as an error would
        # put a warning in the log on every clean exit.
        assert caplog.text == ""


class TestTheDistinctionItself:
    """The asyncio property the three sites rely on.

    This exercises the standard library, not this codebase — it is here so the
    reasoning behind ``return_exceptions=True`` is checkable rather than folklore.
    """

    async def test_gather_returns_the_inner_cancellation_as_a_value(self) -> None:
        inner = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        inner.cancel()

        (outcome,) = await asyncio.gather(inner, return_exceptions=True)

        assert isinstance(outcome, asyncio.CancelledError)

    async def test_gather_still_propagates_the_callers_cancellation(self) -> None:
        inner = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)

        async def _waits() -> None:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            await asyncio.Event().wait()

        task = asyncio.create_task(_waits())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


class _BlocksInCleanup(Actor):
    """An actor whose `on_stop` waits, holding its stopper inside the shield.

    That wait is the window: the caller of `stop()` sits at the shielded await
    for as long as the gate stays closed, so a cancellation can be delivered
    there deliberately rather than hoped for.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "blocks-in-cleanup")
        super().__init__(**kwargs)
        self.entered_cleanup = asyncio.Event()
        self.gate = asyncio.Event()
        self.cleanup_finished = False

    async def on_stop(self) -> None:
        self.entered_cleanup.set()
        await self.gate.wait()
        self.cleanup_finished = True

    async def handle_message(self, msg: Message) -> None:
        return None


class TestTheCallersCancellationSurvivesCleanup:
    """The case the module docstring above called un-hittable.

    It is hittable through the shield rather than through the inner task: an
    `on_stop` that blocks holds the caller at the shielded await indefinitely,
    so the cancellation lands exactly where it must with no race.

    Swallowing it here is what left `Supervisor.stop()` waiting on a watch loop
    that had resumed polling — a hang on shutdown, reproducible roughly once in
    thirty runs, and not only in tests: `ActorSystem.stop_all()` takes the same
    path when the process is going down.
    """

    async def test_the_stopper_still_learns_it_was_cancelled(self, tmp_path: Path) -> None:
        actor = _BlocksInCleanup(persistence_dir=str(tmp_path))
        stopper = asyncio.create_task(actor.stop())
        await actor.entered_cleanup.wait()

        stopper.cancel()
        await asyncio.sleep(0)  # let the cancellation reach the shielded await
        actor.gate.set()

        with pytest.raises(asyncio.CancelledError):
            await stopper

    async def test_cleanup_still_ran_to_completion(self, tmp_path: Path) -> None:
        # The shield earns its place: the cancellation is reported, but not at
        # the cost of the cleanup it was protecting.
        actor = _BlocksInCleanup(persistence_dir=str(tmp_path))
        stopper = asyncio.create_task(actor.stop())
        await actor.entered_cleanup.wait()

        stopper.cancel()
        await asyncio.sleep(0)
        actor.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper

        assert actor.cleanup_finished

    async def test_an_uncancelled_stop_is_unaffected(self, tmp_path: Path) -> None:
        actor = _BlocksInCleanup(persistence_dir=str(tmp_path))
        actor.gate.set()

        await actor.stop()  # must not raise

        assert actor.cleanup_finished


class _Scheduled(Actor):
    """Stands in for ScheduledAgent: a loop task its `on_stop` cancels.

    The loop takes its time to finish cancelling — it catches `CancelledError`,
    waits on a gate, then re-raises. That is what holds `on_stop` inside its
    `gather` long enough for a cancellation aimed at the *caller* to land there,
    which is the window the original audit called unreproducible.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "scheduled")
        super().__init__(**kwargs)
        self._loop_task: asyncio.Task | None = None
        self.cancelling = asyncio.Event()
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def start_loop(self) -> None:
        self._loop_task = asyncio.create_task(self._run())
        await self.entered.wait()

    async def _run(self) -> None:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelling.set()
            await self.release.wait()
            raise

    async def on_stop(self) -> None:
        """The shape under test — mirrors `ScheduledAgent.on_stop`."""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            (outcome,) = await asyncio.gather(self._loop_task, return_exceptions=True)
            if isinstance(outcome, Exception):
                logging.getLogger(__name__).error("loop ended in error: %s", outcome)

    async def handle_message(self, msg: Message) -> None:
        return None


class TestATaskItCancelsIsNotConfusedWithBeingCancelled:
    """The shape a tuple `except` hides.

    `except (asyncio.CancelledError, Exception): pass` cannot tell the loop's own
    cancellation — which the coroutine asked for — from one aimed at whoever
    called `stop()`. Absorbing the second is what left a supervisor's watch loop
    believing it had never been cancelled, and hung shutdown.

    The audit that swept these missed the site because its grep matched only a
    bare `except asyncio.CancelledError`, never a tuple clause.
    """

    async def test_its_own_cancellation_is_not_re_raised(self, tmp_path: Path) -> None:
        # Cancelling the loop is what `on_stop` asked for; surfacing that would
        # make every clean shutdown look like an interruption.
        actor = _Scheduled(persistence_dir=str(tmp_path))
        await actor.start_loop()
        actor.release.set()

        await actor.on_stop()  # must not raise

        assert actor._loop_task is not None and actor._loop_task.done()

    async def test_the_callers_cancellation_survives(self, tmp_path: Path) -> None:
        # The regression. With the old tuple `except`, `on_stop` returned
        # normally here and the caller was told its cancellation had been
        # honoured when it had not.
        actor = _Scheduled(persistence_dir=str(tmp_path))
        await actor.start_loop()
        stopper = asyncio.create_task(actor.on_stop())
        await actor.cancelling.wait()

        stopper.cancel()
        await asyncio.sleep(0)
        actor.release.set()

        with pytest.raises(asyncio.CancelledError):
            await stopper
