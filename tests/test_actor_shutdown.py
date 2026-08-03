"""Stopping an actor must finish before it reports being stopped.

``cancel()`` only requests cancellation. ``stop()`` cancelled the actor's tasks
and returned immediately, so a task part-way through a message unwound after the
actor was already reported stopped. The task list was never cleared either, so
starting the actor again added a second message loop, heartbeat and command
listener beside the ones still winding down — and two command listeners on one
topic means every command is handled twice.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from wactorz.core.actor import Actor, ActorState, Message, MessageType


class _Worker(Actor):
    """Records the messages it was given, and can be held mid-message."""

    def __init__(self, name: str = "worker") -> None:
        super().__init__(name=name)
        self.handled: list[str] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def handle_message(self, message: Message) -> None:
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.handled.append(str(message.payload))


@pytest.fixture(name="worker")
async def worker_fixture() -> AsyncIterator[_Worker]:
    """A started actor, stopped again if the test leaves it running."""
    actor = _Worker()
    await actor.start()
    yield actor
    if actor.state is not ActorState.STOPPED:
        await actor.stop()


class TestStopWaitsForItsTasks:
    async def test_the_actors_own_loops_are_finished(self, worker: _Worker) -> None:
        tasks = list(worker._tasks)
        await worker.stop()
        assert all(task.done() for task in tasks)

    async def test_the_task_list_is_emptied(self, worker: _Worker) -> None:
        await worker.stop()
        assert worker._tasks == []

    async def test_stopping_twice_is_harmless(self, worker: _Worker) -> None:
        await worker.stop()
        await worker.stop()
        assert worker._tasks == []


class TestStartingAgain:
    async def test_a_restarted_actor_runs_one_of_each_loop(self, worker: _Worker) -> None:
        await worker.stop()
        await worker.start()
        try:
            # Three, not six. The stale ones were never removed, so a restarted
            # actor ran two command listeners on the same topic and handled
            # every command twice.
            assert len(worker._tasks) == 3
            assert all(not task.done() for task in worker._tasks)
        finally:
            await worker.stop()

    async def test_a_restarted_actor_processes_messages(self, worker: _Worker) -> None:
        await worker.stop()
        await worker.start()
        try:
            await worker._mailbox.put(
                Message(type=MessageType.TASK, sender_id="test", payload="after restart")
            )
            await asyncio.wait_for(worker.entered.wait(), timeout=2)
        finally:
            await worker.stop()

        assert worker.handled == ["after restart"]


class TestCancellationIsNotSwallowed:
    """Consuming a caller's cancellation strands whoever is waiting on it."""

    async def test_stop_lets_cancellation_through(self, worker: _Worker) -> None:
        started = asyncio.Event()

        async def _stopper() -> None:
            started.set()
            await worker.stop()

        task = asyncio.create_task(_stopper())
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()

        # Without this the error was caught inside the wind-down and dropped:
        # the task carried on as though it had never been cancelled, and
        # whoever awaited it waited for good. This is how the supervisor's
        # watch loop survived its own cancellation and hung Supervisor.stop().
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_the_task_list_is_cleared_even_when_cancelled(self, worker: _Worker) -> None:
        async def _stopper() -> None:
            await worker.stop()

        task = asyncio.create_task(_stopper())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancelled or not, an actor that has stopped must not keep references
        # to the tasks of the run that just ended.
        assert worker._tasks == []


class TestStopDoesNotHang:
    async def test_a_task_that_will_not_unwind_is_given_up_on(self, worker: _Worker) -> None:
        running = asyncio.Event()
        relent = False

        async def _uncancellable() -> None:
            running.set()
            while True:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    if relent:
                        raise
                    # Refuses to stop — a handler swallowing cancellation, or a
                    # library that retries through it.

        stubborn = asyncio.create_task(_uncancellable())
        await running.wait()  # cancelling before it starts would end it cleanly
        worker._tasks.append(stubborn)
        worker.TASK_SHUTDOWN_TIMEOUT = 0.3

        began = time.monotonic()
        try:
            await asyncio.wait_for(worker.stop(), timeout=3)
        finally:
            relent = True
            stubborn.cancel()
            await asyncio.gather(stubborn, return_exceptions=True)
        waited = time.monotonic() - began

        # It waited rather than cancelling and walking away, and it gave up
        # rather than letting one task hold up the whole shutdown.
        assert 0.3 <= waited < 3
        assert worker._tasks == []

    async def test_stopping_from_inside_a_task_does_not_deadlock(self, worker: _Worker) -> None:
        # A stop command arrives on the command listener, which then calls
        # stop() — a task cannot wait for itself.
        async def _stop_from_within() -> None:
            await worker.stop()

        own = asyncio.create_task(_stop_from_within())
        worker._tasks.append(own)

        await asyncio.wait_for(own, timeout=3)
        assert worker.state is ActorState.STOPPED
