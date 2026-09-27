"""Supervisor entries follow the actors they describe: re-armed, freed, read safely.

An entry retired by ``release()`` -- an agent stopped, replaced with new code,
migrated home, or deleted -- stayed retired when an actor of the same name was
spawned again, so the new actor crashed and stayed down with nobody told.
Entries for actors that were gone for good were never freed, each keeping a
factory whose closure held what the actor was built from. A name registered
twice appeared twice in the start order, and a name without an entry stopped
the shutdown loop part-way, leaving every actor after it running and unflushed.
"""

import asyncio

import pytest

from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
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
    registry = ActorRegistry()
    supervisor = Supervisor(registry, _inject, poll_interval=0.01)
    registry._supervisor_ref = supervisor
    return supervisor


async def _parent(supervisor: Supervisor) -> _Worker:
    """An actor that spawns children into the supervisor's registry."""
    parent = _Worker(name="parent")
    parent._registry = supervisor._registry
    return parent


class TestARespawnedAgentIsSupervised:
    async def test_a_name_released_before_is_supervised_again(self, supervisor: Supervisor) -> None:
        # What the retirement message tells a user to do: delete it, spawn it again.
        parent = await _parent(supervisor)
        first = await parent.spawn(_Worker, name="child")
        supervisor.release("child")
        await first.stop()
        await supervisor._registry.unregister(first.actor_id)

        second = await parent.spawn(_Worker, name="child")

        spec = supervisor._specs["child"]
        assert spec.retired is False
        assert spec.actor is second
        assert second.supervisor_id == str(id(supervisor))
        await second.stop()

    async def test_the_new_actor_is_restarted_when_it_crashes(self, supervisor: Supervisor) -> None:
        parent = await _parent(supervisor)
        first = await parent.spawn(_Worker, name="child")
        supervisor.release("child")
        await first.stop()
        await supervisor._registry.unregister(first.actor_id)
        second = await parent.spawn(_Worker, name="child")
        spec = supervisor._specs["child"]
        spec.restart_delay = 0

        second.state = ActorState.FAILED
        await supervisor._supervise_one("child", spec)

        assert spec.actor is not None and spec.actor is not second
        await spec.actor.stop()

    async def test_the_old_actor_s_crashes_are_not_counted_against_it(
        self, supervisor: Supervisor
    ) -> None:
        supervisor.supervise("child", lambda: _Worker(name="child"), max_restarts=2)
        spec = supervisor._specs["child"]
        spec.record_restart()
        spec.record_restart()
        supervisor.release("child")

        supervisor.adopt("child", lambda: _Worker(name="child"), _Worker(name="child"))

        assert supervisor._specs["child"].exhausted is False

    async def test_the_new_factory_replaces_the_old(self, supervisor: Supervisor) -> None:
        # The old factory rebuilds the actor as it was: the code it replaced.
        def old() -> Actor:
            return _Worker(name="child")

        def new() -> Actor:
            return _Worker(name="child")

        supervisor.supervise("child", old)
        supervisor.release("child")

        supervisor.adopt("child", new, _Worker(name="child"))

        assert supervisor._specs["child"].factory is new


class TestAnEmptyRegistryIsStillARegistry:
    def test_it_is_true(self) -> None:
        # `if self._registry:` means "is there one", not "does it hold anyone".
        registry = ActorRegistry()

        assert len(registry) == 0
        assert bool(registry) is True

    async def test_the_first_child_spawned_is_registered_and_supervised(
        self, supervisor: Supervisor
    ) -> None:
        parent = _Worker(name="parent")
        parent._registry = supervisor._registry

        child = await parent.spawn(_Worker, name="child")

        assert supervisor._registry.find_by_name("child") is child
        assert supervisor._specs["child"].actor is child
        await child.stop()


class TestEntriesAreFreed:
    def test_forgetting_frees_the_entry_and_its_place(self, supervisor: Supervisor) -> None:
        actor = _Worker(name="planner-1")
        supervisor.adopt("planner-1", lambda: _Worker(name="planner-1"), actor)

        supervisor.drop_supervised("planner-1")

        assert "planner-1" not in supervisor._specs
        assert "planner-1" not in supervisor._order
        # Or it goes on reporting itself as supervised.
        assert actor.supervisor_id is None

    def test_forgetting_a_name_that_is_not_there_is_harmless(self, supervisor: Supervisor) -> None:
        supervisor.drop_supervised("never-there")

        assert supervisor._specs == {}


class TestTheOrderHoldsEachNameOnce:
    def test_supervising_a_name_again_keeps_one_place(self, supervisor: Supervisor) -> None:
        supervisor.supervise("a", lambda: _Worker(name="a"))
        supervisor.supervise("b", lambda: _Worker(name="b"))

        supervisor.supervise("a", lambda: _Worker(name="a"))

        assert supervisor._order == ["a", "b"]

    def test_adopting_a_name_again_keeps_one_place(self, supervisor: Supervisor) -> None:
        supervisor.adopt("a", lambda: _Worker(name="a"), _Worker(name="a"))
        supervisor.adopt("a", lambda: _Worker(name="a"), _Worker(name="a"))

        assert supervisor._order == ["a"]


class TestAMissingEntryStopsNothing:
    async def test_shutdown_stops_every_actor_around_a_missing_entry(
        self, supervisor: Supervisor
    ) -> None:
        # A KeyError half-way through left every actor after it running.
        first = _Worker(name="first")
        last = _Worker(name="last")
        await first.start()
        await last.start()
        supervisor.adopt("first", lambda: _Worker(name="first"), first)
        supervisor._order.append("gone")
        supervisor.adopt("last", lambda: _Worker(name="last"), last)

        await supervisor.stop()

        assert first.state == ActorState.STOPPED
        assert last.state == ActorState.STOPPED

    def test_status_skips_a_missing_entry(self, supervisor: Supervisor) -> None:
        supervisor.adopt("a", lambda: _Worker(name="a"), _Worker(name="a"))
        supervisor._order.append("gone")

        assert [row["name"] for row in supervisor.status()] == ["a"]

    async def test_rest_for_one_after_its_crashed_name_was_forgotten(
        self, supervisor: Supervisor
    ) -> None:
        supervisor.supervise(
            "a", lambda: _Worker(name="a"), strategy=SupervisorStrategy.REST_FOR_ONE
        )
        spec = supervisor._specs["a"]
        supervisor.drop_supervised("a")

        await supervisor._apply_strategy("a", spec)

        assert supervisor._specs == {}


class TestARestartDoesNotUndoARemoval:
    """The lock is released before a strategy runs, and a restart waits out its delay."""

    async def _crash(
        self, supervisor: Supervisor, delay: float
    ) -> tuple[_Worker, "asyncio.Task[None]"]:
        actor = _Worker(name="worker")
        await actor.start()
        supervisor.adopt("worker", lambda: _Worker(name="worker"), actor, restart_delay=delay)
        spec = supervisor._specs["worker"]
        actor.state = ActorState.FAILED
        return actor, asyncio.create_task(supervisor._supervise_one("worker", spec))

    async def test_a_delete_during_the_delay_is_not_undone(self, supervisor: Supervisor) -> None:
        _actor, restart = await self._crash(supervisor, delay=0.05)
        await asyncio.sleep(0.01)

        supervisor.drop_supervised("worker")
        await restart

        assert supervisor._registry.find_by_name("worker") is None
        assert "worker" not in supervisor._specs

    async def test_a_deliberate_stop_during_the_delay_is_not_undone(
        self, supervisor: Supervisor
    ) -> None:
        _actor, restart = await self._crash(supervisor, delay=0.05)
        await asyncio.sleep(0.01)

        supervisor.release("worker")
        await restart

        assert supervisor._specs["worker"].actor is None
        assert supervisor._registry.find_by_name("worker") is None

    async def test_a_delete_during_the_spawn_stops_what_it_started(
        self, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _actor, _restart = await self._crash(supervisor, delay=0)
        _restart.cancel()
        spec = supervisor._specs["worker"]
        started: list[Actor] = []
        spawn = supervisor._spawn_actor

        async def spawn_then_delete(name: str, spec_: object) -> Actor:
            actor = await spawn(name, spec_)  # pyright: ignore[reportArgumentType]
            started.append(actor)
            supervisor.drop_supervised(name)
            return actor

        monkeypatch.setattr(supervisor, "_spawn_actor", spawn_then_delete)

        await supervisor._restart_one("worker", spec)

        assert started and started[0].state == ActorState.STOPPED
        assert supervisor._registry.find_by_name("worker") is None
