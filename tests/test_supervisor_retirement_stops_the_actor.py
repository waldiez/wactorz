"""Retiring a spec must stop the actor, not just forget it.

Retirement reports the agent as permanently stopped. Clearing ``spec.actor``
without stopping it first made that untrue: ``_stop_actor`` begins by returning
when the spec holds no actor, so the one place that could have stopped it
skipped it, and ``Supervisor.stop()`` walks the same path. The actor's tasks --
its heartbeat among them -- outlived the supervisor that was meant to own them,
and nothing held a reference to stop them by hand.
"""

import asyncio

import pytest

from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.registry import ActorRegistry, Supervisor


class _Worker(Actor):
    """A minimal actor that does nothing but exist."""

    async def handle_message(self, message: Message) -> None:
        return None


def _inject(_actor: Actor) -> None:
    """Stand-in for ActorSystem's MQTT injection, which needs no broker here."""


@pytest.fixture(name="supervisor")
def supervisor_fixture() -> Supervisor:
    """A supervisor over a real registry, with no watch loop running."""
    return Supervisor(ActorRegistry(), _inject, poll_interval=0.01)


async def _retire_a_running_actor(supervisor: Supervisor) -> Actor:
    """Drive a spec to retirement while its actor is running.

    A spec is retired the moment it is found failed with no budget left, which
    happens before the restart path has stopped anything -- so the actor whose
    failure triggers retirement is a started, registered one.
    """
    supervisor.supervise("doomed", lambda: _Worker(name="doomed"), max_restarts=1, restart_delay=0)
    spec = supervisor._specs["doomed"]

    first = _Worker(name="doomed")
    await first.start()
    await supervisor._registry.register(first)
    spec.actor = first
    first.state = ActorState.FAILED

    # Spends the budget and leaves a live replacement in the spec.
    await supervisor._supervise_one("doomed", spec)
    actor = spec.actor
    assert actor is not None and actor is not first
    assert spec.exhausted is True

    actor.state = ActorState.FAILED
    await supervisor._supervise_one("doomed", spec)
    assert spec.retired is True
    return actor


class TestRetirement:
    async def test_the_actor_is_stopped(self, supervisor: Supervisor) -> None:
        actor = await _retire_a_running_actor(supervisor)

        assert actor.state == ActorState.STOPPED

    async def test_no_task_outlives_it(self, supervisor: Supervisor) -> None:
        actor = await _retire_a_running_actor(supervisor)
        await asyncio.sleep(0)

        # The heartbeat loop sleeps between beats, so a task left running is not
        # merely untidy: it wakes on its own schedule for the life of the process.
        assert [task for task in actor._tasks if not task.done()] == []

    async def test_it_is_unregistered(self, supervisor: Supervisor) -> None:
        actor = await _retire_a_running_actor(supervisor)

        assert supervisor._registry.get(actor.actor_id) is None

    async def test_stopping_the_supervisor_is_not_what_stops_it(
        self, supervisor: Supervisor
    ) -> None:
        actor = await _retire_a_running_actor(supervisor)
        await supervisor.stop()

        # stop() walks the specs and reaches _stop_actor, which returns early on a
        # spec with no actor. Retirement cannot defer the stop to it.
        assert actor.state == ActorState.STOPPED
