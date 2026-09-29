"""How the supervisor paces restarts, and what it tells the user.

A crashing actor is restarted after a delay that doubles with each crash in a
row. After ``max_restarts`` of them restarts slow right down, but never stop: an
agent that crashes because something it depends on is down comes back once that
is up again, without anyone having to notice it was gone. Each restart waits in a
task of its own, so one waiting out a long delay leaves the rest supervised.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
from wactorz.core.registry import ActorRegistry, SupervisedSpec, Supervisor

if TYPE_CHECKING:
    from wactorz.agents.main.manifests import ManifestRegistry
    from wactorz.agents.main.nodes import NodeHost


class _Worker(Actor):
    """A minimal actor that does nothing but exist."""

    async def handle_message(self, message: Message) -> None:
        return None


def _inject(_actor: Actor) -> None:
    """Stand-in for ActorSystem's MQTT injection, which needs no broker here."""


class _Notices:
    """Stands in for ``Supervisor._notify_main``, keeping what it was told."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, message: str, severity: str = "critical") -> None:
        self.sent.append((severity, message))

    @property
    def severities(self) -> list[str]:
        return [severity for severity, _ in self.sent]


@pytest.fixture(name="notices")
def notices_fixture() -> _Notices:
    return _Notices()


@pytest.fixture(name="supervisor")
def supervisor_fixture(notices: _Notices, monkeypatch: pytest.MonkeyPatch) -> Supervisor:
    """A supervisor over a real registry, with no watch loop running."""
    supervisor = Supervisor(ActorRegistry(), _inject, poll_interval=0.01)
    monkeypatch.setattr(supervisor, "_notify_main", notices)
    return supervisor


def _spec(supervisor: Supervisor, name: str = "w", **kwargs: Any) -> SupervisedSpec:
    kwargs.setdefault("restart_delay", 0)
    supervisor.supervise(name, lambda: _Worker(name=name), **kwargs)
    return supervisor._specs[name]


def _crashed(spec: SupervisedSpec, name: str = "w") -> None:
    spec.actor = _Worker(name=name)
    spec.actor.state = ActorState.FAILED


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    """Wait for ``predicate`` to hold, polling; fail rather than hang."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def _watch(supervisor: Supervisor) -> None:
    """Run the watch loop over the actors as set up, rather than starting new ones."""
    supervisor._watch_task = asyncio.create_task(supervisor._watch_loop())


class TestTheDelay:
    def test_it_doubles_with_each_crash_in_a_row(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_delay=2, max_restarts=10)

        delays = []
        for streak in range(1, 5):
            spec.crash_streak = streak
            delays.append(supervisor._restart_delay(spec, crashed=True))

        assert delays == [2, 4, 8, 16]

    def test_it_is_capped(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_delay=2, max_restarts=100)
        spec.crash_streak = 30

        assert supervisor._restart_delay(spec, crashed=True) == Supervisor.MAX_RESTART_DELAY

    def test_slowed_down_it_starts_again_from_the_slow_delay_and_doubles(
        self, supervisor: Supervisor
    ) -> None:
        spec = _spec(supervisor, restart_delay=2, max_restarts=3)
        spec.slow = True

        delays = []
        for streak in range(4, 12):
            spec.crash_streak = streak
            delays.append(supervisor._restart_delay(spec, crashed=True))

        slow = Supervisor.SLOW_RETRY_DELAY
        assert delays[:3] == [slow, slow * 2, slow * 4]
        assert delays[-1] == Supervisor.MAX_SLOW_RETRY_DELAY

    def test_a_sibling_waits_only_its_own_delay(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_delay=2)
        spec.crash_streak = 4
        spec.slow = True

        assert supervisor._restart_delay(spec, crashed=False) == 2


class TestRepeatedCrashes:
    async def test_restarts_slow_down_and_never_stop(
        self, supervisor: Supervisor, notices: _Notices, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supervisor, "SLOW_RETRY_DELAY", 0)
        spec = _spec(supervisor, max_restarts=2)

        for _ in range(6):
            _crashed(spec)
            await supervisor._supervise_one("w", spec)

        assert spec.slow is True
        assert spec.retired is False
        assert spec.actor is not None and spec.actor.state == ActorState.RUNNING
        assert spec.restarts == 6
        await spec.actor.stop()

    async def test_each_quick_restart_is_reported_and_slowing_down_once(
        self, supervisor: Supervisor, notices: _Notices, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supervisor, "SLOW_RETRY_DELAY", 0)
        spec = _spec(supervisor, max_restarts=2)

        for _ in range(5):
            _crashed(spec)
            await supervisor._supervise_one("w", spec)

        # Two quick restarts, then one notice that they are slowing, then quiet:
        # a notice per slow attempt would be a notice an hour, for days.
        assert notices.severities == ["warning", "warning", "critical"]
        assert spec.actor is not None
        await spec.actor.stop()

    async def test_the_actor_reports_how_often_it_was_restarted(
        self, supervisor: Supervisor
    ) -> None:
        spec = _spec(supervisor)

        for _ in range(3):
            _crashed(spec)
            await supervisor._supervise_one("w", spec)

        assert spec.actor is not None and spec.actor.metrics.restart_count == 3
        assert supervisor.status()[0]["restarts_used"] == 3
        await spec.actor.stop()

    async def test_slowed_down_actors_are_listed(self, supervisor: Supervisor) -> None:
        _spec(supervisor, "steady")
        _spec(supervisor, "flapping").slow = True

        assert supervisor.slow_retrying() == ["flapping"]
        assert [row["slow_retry"] for row in supervisor.status()] == [False, True]


class TestRecovery:
    def _running(self, spec: SupervisedSpec, up_for: float) -> Actor:
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        actor.metrics.start_time = time.time() - up_for
        actor.metrics.last_heartbeat = time.time()
        spec.actor = actor
        return actor

    def test_the_streak_ends_once_the_actor_stays_up(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_window=60)
        spec.crash_streak = 3
        self._running(spec, up_for=61)

        # Not a recovery from slow retry, so nothing to announce.
        assert supervisor._note_health(spec, time.time()) is False
        assert spec.crash_streak == 0

    def test_it_does_not_end_while_the_actor_is_young(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_window=60)
        spec.crash_streak = 3
        self._running(spec, up_for=10)

        supervisor._note_health(spec, time.time())

        assert spec.crash_streak == 3

    def test_leaving_slow_retry_takes_as_long_as_the_last_wait(
        self, supervisor: Supervisor
    ) -> None:
        # Or an agent that runs a few minutes between crashes leaves slow retry
        # and enters it again, with a notice each time.
        spec = _spec(supervisor, restart_window=60, max_restarts=2)
        spec.crash_streak = 3
        spec.slow = True
        actor = self._running(spec, up_for=Supervisor.SLOW_RETRY_DELAY / 2)

        assert supervisor._note_health(spec, time.time()) is False
        assert spec.slow is True

        actor.metrics.start_time -= Supervisor.SLOW_RETRY_DELAY / 2
        assert supervisor._note_health(spec, time.time()) is True
        assert (spec.slow, spec.crash_streak) == (False, 0)

    async def test_a_recovery_is_reported(self, supervisor: Supervisor, notices: _Notices) -> None:
        spec = _spec(supervisor, restart_window=0, max_restarts=2)
        spec.crash_streak = 3
        spec.slow = True
        self._running(spec, up_for=Supervisor.MAX_SLOW_RETRY_DELAY)

        _watch(supervisor)
        try:
            await _until(lambda: bool(notices.sent))
        finally:
            await supervisor.stop()

        assert notices.severities == ["info"]
        assert "back to normal" in notices.sent[0][1]


class TestTheErrorStorm:
    def test_errors_spread_over_time_are_not_a_storm(self, supervisor: Supervisor) -> None:
        # Counted over a lifetime, one handled error an hour restarts the agent
        # every few hours.
        spec = _spec(supervisor)
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor
        start = time.time()

        for minute in range(Supervisor.ERROR_STORM_THRESHOLD * 2):
            actor.metrics.errors += 1
            supervisor._note_health(spec, start + minute * Supervisor.ERROR_STORM_WINDOW)

        assert supervisor._failure_reason(spec) is None

    def test_errors_close_together_are(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor)
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor
        start = time.time()

        for second in range(Supervisor.ERROR_STORM_THRESHOLD):
            actor.metrics.errors += 1
            supervisor._note_health(spec, start + second)

        assert "error storm" in (supervisor._failure_reason(spec) or "")

    def test_a_count_that_was_reset_is_read_as_new(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor)
        actor = _Worker(name="w")
        actor.state = ActorState.RUNNING
        spec.actor = actor
        spec._errors_seen = 50

        actor.metrics.errors = 3
        supervisor._note_health(spec, time.time())

        assert (len(spec._error_times), spec._errors_seen) == (3, 3)


class TestSiblings:
    async def test_only_the_crashed_actor_counts_a_crash(self, supervisor: Supervisor) -> None:
        crashed = _spec(supervisor, "a", strategy=SupervisorStrategy.ONE_FOR_ALL)
        sibling = _spec(supervisor, "b", strategy=SupervisorStrategy.ONE_FOR_ALL)
        _crashed(crashed, "a")
        sibling.actor = _Worker(name="b")
        await sibling.actor.start()

        await supervisor._supervise_one("a", crashed)

        assert (crashed.crash_streak, sibling.crash_streak) == (1, 0)
        assert (crashed.restarts, sibling.restarts) == (1, 1)
        await supervisor.stop()


class TestAStrategyHoldsItsGroup:
    async def test_a_sibling_waiting_its_turn_is_not_restarted_twice(
        self, supervisor: Supervisor
    ) -> None:
        # Stopped, with no actor, until the strategy reaches it -- which the
        # watch loop would otherwise take for a crash of its own.
        built: list[str] = []

        def factory(name: str) -> Any:
            def build() -> Actor:
                built.append(name)
                return _Worker(name=name)

            return build

        for name, delay in (("a", 0.0), ("b", 0.1)):
            supervisor.supervise(
                name, factory(name), strategy=SupervisorStrategy.ONE_FOR_ALL, restart_delay=delay
            )
        await supervisor.start()
        built.clear()
        a = supervisor._specs["a"]
        assert a.actor is not None
        a.actor.state = ActorState.FAILED

        await _until(lambda: built == ["a", "b"])
        await asyncio.sleep(0.05)  # several more polls
        await supervisor.stop()

        assert built == ["a", "b"]
        assert supervisor._specs["b"].crash_streak == 0


class TestTheCrashedActorIsStoppedFirst:
    async def test_it_is_stopped_before_the_wait_not_after_it(self, supervisor: Supervisor) -> None:
        # A FAILED actor's subscriptions, windows and command listener run on
        # until it is stopped, and a wait in slow retry is minutes to an hour.
        spec = _spec(supervisor, restart_delay=30)
        _crashed(spec)
        old = spec.actor
        assert old is not None
        await supervisor._registry.register(old)

        _watch(supervisor)
        try:
            # Cleared last, once the actor is stopped and unregistered.
            await _until(lambda: spec.actor is None)

            assert old.state == ActorState.STOPPED
            assert supervisor._registry.find_by_name("w") is None
            assert spec.restarting is True  # still waiting out its delay
        finally:
            await supervisor.stop()


class TestRestartsRunOnTheirOwn:
    async def test_a_long_delay_does_not_hold_up_the_others(self, supervisor: Supervisor) -> None:
        waiting = _spec(supervisor, "waiting", restart_delay=30)
        quick = _spec(supervisor, "quick")
        _crashed(waiting, "waiting")
        _crashed(quick, "quick")
        first = quick.actor

        _watch(supervisor)
        try:
            await _until(lambda: quick.actor not in (first, None))
            assert waiting.restarting is True
        finally:
            await supervisor.stop()

    async def test_a_pending_restart_is_not_started_twice(self, supervisor: Supervisor) -> None:
        spec = _spec(supervisor, restart_delay=30)
        _crashed(spec)

        _watch(supervisor)
        try:
            await _until(lambda: spec.restarting)
            task = spec._restart_task
            await asyncio.sleep(0.05)  # several polls

            assert spec._restart_task is task
            assert spec.crash_streak == 1
        finally:
            await supervisor.stop()

    async def test_stopping_cancels_a_restart_waiting_out_its_delay(
        self, supervisor: Supervisor
    ) -> None:
        spawned: list[Actor] = []

        def factory() -> Actor:
            actor = _Worker(name="w")
            spawned.append(actor)
            return actor

        supervisor.supervise("w", factory, restart_delay=30)
        spec = supervisor._specs["w"]
        _crashed(spec)
        _watch(supervisor)
        await _until(lambda: spec.restarting)
        restart = spec._restart_task

        await asyncio.wait_for(supervisor.stop(), 2)

        assert restart is not None and restart.cancelled()
        assert spawned == []


class TestNothingIsLeftBehind:
    async def test_forgetting_a_spec_cancels_its_pending_restart(
        self, supervisor: Supervisor
    ) -> None:
        # Out of _specs, stop() can no longer reach it.
        spec = _spec(supervisor, restart_delay=30)
        _crashed(spec)
        _watch(supervisor)
        await _until(lambda: spec.restarting)
        restart = spec._restart_task
        assert restart is not None

        supervisor.drop_supervised("w")
        await asyncio.gather(restart, return_exceptions=True)

        assert restart.cancelled()
        await supervisor.stop()

    async def test_a_start_cut_short_stops_and_unregisters_the_actor(
        self, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Registered, but never handed back to be stopped by anyone.
        started = asyncio.Event()
        built: list[Actor] = []

        async def slow_start(self: Actor) -> None:
            built.append(self)
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(_Worker, "start", slow_start)
        spec = _spec(supervisor)
        spawn = asyncio.create_task(supervisor._spawn_actor("w", spec))
        await started.wait()

        spawn.cancel()
        await asyncio.gather(spawn, return_exceptions=True)

        assert supervisor._registry.find_by_name("w") is None
        assert built[0].state == ActorState.STOPPED


class _Host:
    def __init__(self) -> None:
        self.notices: list[dict[str, Any]] = []

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notices.append(notice)


def _nodes() -> tuple[NodeManager, _Host]:
    host = _Host()
    manager = NodeManager(cast("NodeHost", host), cast("ManifestRegistry", object()))
    return manager, host


def _beat(slow: list[str], agents: list[str] | None = None) -> dict[str, Any]:
    return {"agents": agents if agents is not None else ["cam", "probe"], "slow_retry": slow}


class TestANodeSAgents:
    """A node's supervisor has no main beside it; main hears through the heartbeat."""

    async def test_the_heartbeat_list_is_kept(self) -> None:
        nodes = NodeManager()

        await nodes.receive_heartbeat("rpi", {"agents": ["cam"], "slow_retry": ["cam", 7]})

        assert nodes.known["rpi"]["slow_retry"] == ["cam"]

    async def test_a_heartbeat_without_it_reports_none(self) -> None:
        nodes = NodeManager()

        await nodes.receive_heartbeat("rpi", {"agents": ["cam"]})

        assert nodes.known["rpi"]["slow_retry"] == []

    def test_slowing_down_is_said_once(self) -> None:
        nodes, host = _nodes()

        nodes.follow_restarts("rpi", _beat(["cam"]), _beat([]))
        nodes.follow_restarts("rpi", _beat(["cam"]), _beat(["cam"]))

        assert [n["severity"] for n in host.notices] == ["critical"]
        assert "cam" in host.notices[0]["message"] and "rpi" in host.notices[0]["message"]

    def test_recovering_is_said(self) -> None:
        nodes, host = _nodes()

        nodes.follow_restarts("rpi", _beat([]), _beat(["cam"]))

        assert [n["severity"] for n in host.notices] == ["info"]

    def test_a_runner_that_restarted_has_not_recovered_anything(self) -> None:
        # It starts every agent afresh, so its empty list says nothing.
        nodes, host = _nodes()

        nodes.follow_restarts(
            "rpi", {**_beat([]), "pid": 2, "uptime_s": 5}, {**_beat(["cam"]), "pid": 1}
        )
        nodes.follow_restarts(
            "rpi",
            {**_beat([]), "pid": 1, "uptime_s": 5},
            {**_beat(["cam"]), "pid": 1, "uptime_s": 900},
        )

        assert host.notices == []

    def test_the_same_run_recovering_is_said(self) -> None:
        nodes, host = _nodes()

        nodes.follow_restarts(
            "rpi",
            {**_beat([]), "pid": 1, "uptime_s": 910},
            {**_beat(["cam"]), "pid": 1, "uptime_s": 900},
        )

        assert [n["severity"] for n in host.notices] == ["info"]

    def test_an_agent_that_is_gone_has_not_recovered(self) -> None:
        nodes, host = _nodes()

        nodes.follow_restarts("rpi", _beat([], agents=["probe"]), _beat(["cam"]))

        assert host.notices == []
