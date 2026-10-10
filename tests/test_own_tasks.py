"""A run on a loop it shares with a host stops its own tasks and leaves the host's,
whenever the host started them."""

import asyncio
import contextvars
from typing import Any, cast

import pytest

from wactorz import app as app_module
from wactorz.core import own_tasks


async def _forever() -> None:
    await asyncio.sleep(3600)


class TestTheTagger:
    async def test_tasks_made_inside_the_run_are_marked_and_the_hosts_are_not(self) -> None:
        before = asyncio.create_task(_forever())
        tagger = own_tasks.start()
        try:
            ours = asyncio.create_task(_forever())

            async def nested() -> asyncio.Task[None]:
                return asyncio.create_task(_forever())

            # A task of ours making a task: the mark is inherited.
            grandchild = await asyncio.create_task(nested())
        finally:
            own_tasks.stop(tagger)
        # The host, after the run has started, from its own context.
        after = asyncio.create_task(_forever())

        assert tagger.tasks() == {ours, grandchild}
        for task in (before, ours, grandchild, after):
            task.cancel()
        await asyncio.gather(before, ours, grandchild, after, return_exceptions=True)

    async def test_a_host_task_made_during_the_run_is_not_ours(self) -> None:
        """What a notebook cell does after `serve` has started: the task is the host's."""
        tagger = own_tasks.start()
        ours = asyncio.create_task(_forever())
        try:
            # Created from a context the run never touched, the host's own.
            loop = asyncio.get_running_loop()
            hosts_task: list[asyncio.Task[None]] = []
            loop.call_soon(
                lambda: hosts_task.append(asyncio.create_task(_forever())),
                context=_fresh_context(),
            )
            await asyncio.sleep(0)

            assert ours in tagger.tasks()
            assert hosts_task and hosts_task[0] not in tagger.tasks()
        finally:
            own_tasks.stop(tagger)
            for task in (ours, *hosts_task):
                task.cancel()
            await asyncio.gather(ours, *hosts_task, return_exceptions=True)

    async def test_the_previous_factory_is_kept_and_put_back(self) -> None:
        loop = asyncio.get_running_loop()
        made: list[object] = []

        def factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> Any:
            made.append(coro)
            return asyncio.Task(coro, loop=loop, **kwargs)

        # The stub's signature is the loop's, not the one typeshed narrows it to.
        loop.set_task_factory(cast(Any, factory))
        try:
            tagger = own_tasks.start()
            task = asyncio.create_task(_forever())
            own_tasks.stop(tagger)
            assert loop.get_task_factory() is factory
            assert made and task in tagger.tasks()
        finally:
            loop.set_task_factory(None)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_outside_a_run_every_task_counts(self) -> None:
        task = asyncio.create_task(_forever())
        try:
            assert task in own_tasks.own_tasks()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _fresh_context() -> contextvars.Context:
    """A context no run has touched: what a host's own code runs in."""
    return contextvars.Context()


class TestTheShutdownSweep:
    async def test_it_stops_ours_and_spares_the_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.core import cancellation

        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)
        hosts_before = asyncio.create_task(_forever())
        tagger = own_tasks.start()
        try:
            ours = asyncio.create_task(_forever())
            hosts_after: list[asyncio.Task[None]] = []
            asyncio.get_running_loop().call_soon(
                lambda: hosts_after.append(asyncio.create_task(_forever())),
                context=_fresh_context(),
            )
            await asyncio.sleep(0)

            await asyncio.wait_for(app_module._stop_leftover_tasks(), timeout=5.0)

            assert ours.cancelled()
            assert not hosts_before.cancelled()
            assert hosts_after and not hosts_after[0].cancelled()
        finally:
            own_tasks.stop(tagger)
            for task in (hosts_before, *hosts_after):
                task.cancel()
            await asyncio.gather(hosts_before, *hosts_after, return_exceptions=True)
