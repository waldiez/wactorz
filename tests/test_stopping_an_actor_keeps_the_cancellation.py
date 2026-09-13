"""Stopping an actor never consumes the caller's own cancellation.

`Actor.stop()` cancels the actor's tasks and waits for them. Whoever called it
may itself be cancelled while that wait is in progress — the Supervisor's watch
loop is, every time `Supervisor.stop()` runs while a restart is under way. That
cancellation belongs to the caller and has to survive.

On Python 3.10 it did not. `asyncio.wait_for` returns the result from inside its
own `CancelledError` handler when the future it guards has completed:

    except exceptions.CancelledError:
        if fut.done():
            return fut.result()

The wait here sets that condition up rather than stumbling into it — the tasks
are cancelled on the line above, so the gather over them completes at once, and
a cancellation arriving in that window is discarded. Nothing was logged, because
`wait_for` returned a value rather than raising: the watch loop went back to
polling and `Supervisor.stop()`, which waits on it without a timeout, never
returned.

These tests pin the guarantee rather than the mechanism, so a later rewrite is
free as long as a cancellation still gets through.
"""

import asyncio
from pathlib import Path

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import Actor, ActorState, Message

TICKING = """
async def process(agent):
    pass
"""


class Idle(Actor):
    """An actor whose tasks sit in a long sleep, as a real one's loops do."""

    async def handle_message(self, msg: Message) -> None:
        return None

    async def _sleeper(self) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return


def make_idle(tmp_path: Path, task_count: int = 3) -> Idle:
    actor = Idle(name="idle", persistence_dir=str(tmp_path))
    actor.state = ActorState.RUNNING
    actor._tasks = [asyncio.ensure_future(actor._sleeper()) for _ in range(task_count)]
    return actor


async def cancel_while_winding_down(wind_down: object) -> bool:
    """Cancel the task running `wind_down` and report whether it noticed.

    The cancel is issued from a callback scheduled on the loop, so it lands
    while the wind-down is between its own await points — which is where the
    caller's cancellation used to be discarded.
    """
    noticed = {"value": False}

    async def caller() -> None:
        try:
            await wind_down()  # pyright: ignore[reportCallIssue]
            # Getting here means the cancellation was consumed. Keep going the
            # way the watch loop does, so a lost cancellation shows up as a task
            # that will not finish rather than as one that quietly returned.
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            noticed["value"] = True
            raise

    task = asyncio.ensure_future(caller())
    await asyncio.sleep(0)  # let it reach the wind-down
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return noticed["value"]


class TestActorWindDown:
    async def test_a_cancellation_reaches_the_caller(self, tmp_path: Path) -> None:
        actor = make_idle(tmp_path)

        assert await cancel_while_winding_down(actor._wind_down_tasks)

    async def test_the_caller_task_actually_finishes(self, tmp_path: Path) -> None:
        """The symptom, stated as the supervisor saw it: a task awaited without a
        timeout after being cancelled has to end, or the wait is forever."""
        actor = make_idle(tmp_path)

        async def caller() -> None:
            await actor._wind_down_tasks()
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(caller())
        await asyncio.sleep(0)
        task.cancel()

        # `Supervisor.stop()` gathers the watch task with no timeout; one here
        # turns a hang into a failure instead of a stalled suite.
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5.0)
        assert task.done()

    async def test_the_tasks_are_still_stopped(self, tmp_path: Path) -> None:
        """The wind-down's own job is unchanged."""
        actor = make_idle(tmp_path)
        tasks = list(actor._tasks)

        await actor._wind_down_tasks()

        assert all(task.done() for task in tasks)
        assert not actor._tasks

    async def test_a_task_that_will_not_stop_is_not_waited_on_for_ever(
        self, tmp_path: Path
    ) -> None:
        """The bound the timeout exists for, kept."""
        actor = Idle(name="stubborn", persistence_dir=str(tmp_path))
        actor.state = ActorState.RUNNING
        give_up = asyncio.Event()

        async def uncancellable() -> None:
            while not give_up.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass  # refuses to unwind, the case the timeout is for

        stubborn = asyncio.ensure_future(uncancellable())
        actor._tasks = [stubborn]
        actor.TASK_SHUTDOWN_TIMEOUT = 0.05
        try:
            await asyncio.wait_for(actor._wind_down_tasks(), timeout=5.0)
        finally:
            # The task outlives the wind-down by design; only the test can end
            # it, and leaving it running fails the loop's teardown.
            give_up.set()
            stubborn.cancel()
            await asyncio.gather(stubborn, return_exceptions=True)


class TestDynamicAgentTearDown:
    """The same wait guards an in-place repair, which starts from inside the
    process loop it is replacing — so the cancellation matters there too."""

    async def test_a_cancellation_reaches_the_caller(self, tmp_path: Path) -> None:
        agent = DynamicAgent(
            name="repairing",
            code=TICKING,
            poll_interval=0,
            persistence_dir=str(tmp_path),
        )
        agent.state = ActorState.RUNNING

        async def sleeper() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        agent._program_tasks = [asyncio.ensure_future(sleeper()) for _ in range(3)]

        assert await cancel_while_winding_down(agent._tear_down_program)

    async def test_the_program_tasks_are_still_stopped(self, tmp_path: Path) -> None:
        agent = DynamicAgent(
            name="repairing",
            code=TICKING,
            poll_interval=0,
            persistence_dir=str(tmp_path),
        )
        agent.state = ActorState.RUNNING

        async def sleeper() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        tasks = [asyncio.ensure_future(sleeper()) for _ in range(3)]
        agent._program_tasks = list(tasks)

        await agent._tear_down_program()

        assert all(task.done() for task in tasks)
        assert not agent._program_tasks


@pytest.mark.parametrize("task_count", [1, 5])
async def test_it_holds_however_many_tasks_there_are(tmp_path: Path, task_count: int) -> None:
    actor = make_idle(tmp_path, task_count=task_count)

    assert await cancel_while_winding_down(actor._wind_down_tasks)
