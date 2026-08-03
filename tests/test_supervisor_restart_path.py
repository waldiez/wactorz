"""A restart that fails must not end supervision, and must not stall it.

Two defects in the same twenty lines. ``_stop_actor`` clears ``spec.actor``
before the respawn, so a factory that raised left the spec holding None — and
the watch loop skipped specs with no actor, permanently. One bad restart and the
agent was gone for the life of the process, reported by a single log line.

The lock made it worse: it was held across the whole poll, including the restart
delay, the stop and the spawn. One slow actor stalled supervision of every other
one, so a restart that hung rather than raised took the supervisor with it.
"""

import asyncio
import time

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


class TestAFailedRespawn:
    async def test_the_spec_is_retried_rather_than_abandoned(self, supervisor: Supervisor) -> None:
        attempts = {"n": 0}

        def _factory() -> Actor:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("dependency not ready")
            return _Worker(name="flaky")

        supervisor.supervise("flaky", _factory, max_restarts=5, restart_delay=0)
        spec = supervisor._specs["flaky"]
        spec.actor = _Worker(name="flaky")
        spec.actor.state = ActorState.FAILED

        await supervisor._supervise_one("flaky", spec)
        # The factory raised, so there is no actor — but the spec is still live.
        assert spec.actor is None
        assert spec.retired is False

        # The next poll must pick it up again. Without that, "no actor" was a
        # state nothing ever moved out of.
        await supervisor._supervise_one("flaky", spec)
        assert spec.actor is not None
        assert attempts["n"] == 2

    async def test_a_spec_with_no_actor_is_detected(self, supervisor: Supervisor) -> None:
        supervisor.supervise("gone", lambda: _Worker(name="gone"), restart_delay=0)
        spec = supervisor._specs["gone"]
        spec.actor = None

        assert await supervisor._detect_failures() == [("gone", spec)]

    async def test_a_persistently_failing_factory_retires_and_reports(
        self, supervisor: Supervisor
    ) -> None:
        def _factory() -> Actor:
            raise RuntimeError("will never start")

        supervisor.supervise("doomed", _factory, max_restarts=2, restart_delay=0)
        spec = supervisor._specs["doomed"]
        spec.actor = _Worker(name="doomed")
        spec.actor.state = ActorState.FAILED

        for _ in range(5):
            await supervisor._supervise_one("doomed", spec)

        # Retrying forever would be as bad as giving up silently: the budget
        # bounds it, and retirement is announced rather than assumed.
        assert spec.retired is True
        assert len(spec._restart_times) <= spec.max_restarts

    async def test_a_retired_spec_is_left_alone(self, supervisor: Supervisor) -> None:
        supervisor.supervise("stopped", lambda: _Worker(name="stopped"), restart_delay=0)
        spec = supervisor._specs["stopped"]
        spec.retired = True
        spec.actor = None

        # release() retires a spec and clears its actor. If "no actor" alone
        # triggered a restart, every deliberate stop would be undone.
        assert await supervisor._detect_failures() == []


class TestTheLockIsNotHeldAcrossRestarts:
    async def test_a_slow_restart_does_not_stall_the_other_specs(
        self, supervisor: Supervisor
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_factory() -> Actor:
            started.set()
            await release.wait()
            return _Worker(name="slow")

        supervisor.supervise("slow", _slow_factory, restart_delay=0)
        supervisor.supervise("quick", lambda: _Worker(name="quick"), restart_delay=0)
        slow, quick = supervisor._specs["slow"], supervisor._specs["quick"]
        slow.actor = None
        quick.actor = None

        restart = asyncio.create_task(supervisor._supervise_one("slow", slow))
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            # Held across the restart, this call never returns until the slow
            # actor finishes starting — which is how one stuck actor blinded the
            # supervisor to every other.
            await asyncio.wait_for(supervisor._detect_failures(), timeout=0.5)
        finally:
            release.set()
            await restart

    async def test_a_spec_retired_mid_restart_is_not_resurrected(
        self, supervisor: Supervisor
    ) -> None:
        supervisor.supervise("doomed", lambda: _Worker(name="doomed"), restart_delay=0.05)
        spec = supervisor._specs["doomed"]
        spec.actor = None

        restart = asyncio.create_task(supervisor._supervise_one("doomed", spec))
        await asyncio.sleep(0)
        supervisor.release("doomed")  # a stop command arriving mid-restart
        await restart

        # The re-check happens before the work, so this one still completes;
        # what must hold is that the spec stays retired and the watch loop
        # will not pick it up again.
        assert spec.retired is True
        assert await supervisor._detect_failures() == []

    async def test_stopping_abandons_an_in_flight_restart(self, supervisor: Supervisor) -> None:
        release = asyncio.Event()
        spawned: list[Actor] = []

        async def _factory() -> Actor:
            if spawned:  # the restart, not the initial start
                await release.wait()
            actor = _Worker(name="slow")
            spawned.append(actor)
            return actor

        supervisor.supervise("slow", _factory, restart_delay=0)
        await supervisor.start()
        spawned[0].state = ActorState.FAILED
        await asyncio.sleep(0.05)  # let the watch loop begin the restart

        await asyncio.wait_for(supervisor.stop(), timeout=2)

        # stop() cancels the watch loop and waits for it to unwind. Without the
        # wait it returned while the restart was still running, and the actor it
        # then produced joined a system that had already shut down.
        assert supervisor._watch_task is None
        assert len(spawned) == 1
        assert all(a.state is not ActorState.RUNNING for a in spawned)
        release.set()


class TestDetectionIsUnchanged:
    """Narrowing the lock must not change which failures are found."""

    async def test_a_failed_actor_is_detected(self, supervisor: Supervisor) -> None:
        supervisor.supervise("w", lambda: _Worker(name="w"), restart_delay=0)
        spec = supervisor._specs["w"]
        spec.actor = _Worker(name="w")
        spec.actor.state = ActorState.FAILED

        assert supervisor._failure_reason(spec) is not None

    async def test_a_stopped_or_paused_actor_is_not_a_failure(self, supervisor: Supervisor) -> None:
        supervisor.supervise("w", lambda: _Worker(name="w"), restart_delay=0)
        spec = supervisor._specs["w"]
        spec.actor = _Worker(name="w")

        for state in (ActorState.STOPPED, ActorState.PAUSED):
            spec.actor.state = state
            assert supervisor._failure_reason(spec) is None

    async def test_a_silent_actor_is_detected_once_it_is_past_warm_up(
        self, supervisor: Supervisor
    ) -> None:
        supervisor.supervise("w", lambda: _Worker(name="w"), restart_delay=0)
        spec = supervisor._specs["w"]
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor

        now = time.time()
        actor.metrics.start_time = now - Supervisor.HEARTBEAT_TIMEOUT * 3
        actor.metrics.last_heartbeat = now - 1
        assert supervisor._failure_reason(spec) is None

        actor.metrics.last_heartbeat = now - Supervisor.HEARTBEAT_TIMEOUT - 1
        assert "heartbeat" in (supervisor._failure_reason(spec) or "")

    async def test_a_young_silent_actor_is_given_time(self, supervisor: Supervisor) -> None:
        supervisor.supervise("w", lambda: _Worker(name="w"), restart_delay=0)
        spec = supervisor._specs["w"]
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor
        actor.metrics.start_time = time.time()
        actor.metrics.last_heartbeat = 0.0

        # Starting up is not crashing.
        assert supervisor._failure_reason(spec) is None

    async def test_an_error_storm_is_detected(self, supervisor: Supervisor) -> None:
        supervisor.supervise("w", lambda: _Worker(name="w"), restart_delay=0)
        spec = supervisor._specs["w"]
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor

        actor.metrics.errors = Supervisor.ERROR_STORM_THRESHOLD - 1
        assert supervisor._failure_reason(spec) is None

        actor.metrics.errors = Supervisor.ERROR_STORM_THRESHOLD
        assert "error storm" in (supervisor._failure_reason(spec) or "")
