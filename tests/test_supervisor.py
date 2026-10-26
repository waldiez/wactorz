"""
test_supervisor.py — Logical tests for the Wactorz supervision tree.

Tests run fully in-process. No MQTT broker required, no real LLM calls,
no network. Each test creates a minimal ActorSystem, registers actors under
the Supervisor, and verifies behaviour by inspecting actor state.
"""

import asyncio
import tempfile
import time
from collections.abc import Callable
from typing import Any

from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
from wactorz.core.registry import ActorSystem, Supervisor

# ── Helpers ───────────────────────────────────────────────────────────────────

# Every actor built here writes state somewhere. A fixed path under /tmp is
# shared by every run at once — a second run of the suite, or a second checkout,
# reads and writes the same files, so each run gets a directory of its own,
# held at module scope so it is cleaned up when the process exits.
_STATE = tempfile.TemporaryDirectory(prefix="wactorz-supervisor-test-")
STATE_DIR = _STATE.name


def assert_eq(label: str, got: Any, expected: Any) -> bool:
    ok = bool(got == expected)
    assert ok, f"{label}: got={got!r}, expected={expected!r}"
    return ok


def assert_true(label: str, value: Any, detail: str = "") -> bool:
    ok = bool(value)
    assert ok, f"{label}: {detail or value}"
    return ok


# ── Minimal test actors ───────────────────────────────────────────────────────


class StableActor(Actor):
    """An actor that stays healthy forever."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "stable-actor")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self) -> None:
        self.started_count += 1

    async def handle_message(self, msg: Message) -> None:
        pass


class CrashOnceActor(Actor):
    """Crashes on first on_start. crash_state is controlled externally via closure."""

    def __init__(self, should_crash: bool = False, **kwargs: Any) -> None:
        kwargs.setdefault("name", "crash-once")
        super().__init__(**kwargs)
        self.started_count = 0
        self._should_crash = should_crash  # set by factory closure

    async def on_start(self) -> None:
        self.started_count += 1
        if self._should_crash:
            self.state = ActorState.FAILED

    async def handle_message(self, msg: Message) -> None:
        pass


class AlwaysCrashActor(Actor):
    """Crashes every time it starts — drives restarts into slow retry."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "always-crash")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self) -> None:
        self.started_count += 1
        self.state = ActorState.FAILED

    async def handle_message(self, msg: Message) -> None:
        pass


class DependentActor(Actor):
    """Simulates an actor that depends on a sibling being healthy."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "dependent")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self) -> None:
        self.started_count += 1

    async def handle_message(self, msg: Message) -> None:
        pass


# ── Test infrastructure ───────────────────────────────────────────────────────


def running_actor(system: Any, name: str) -> Any:
    """The actor the supervisor holds for ``name``, once it is started; else None.

    What a test of a restart waits for. Not the factory having been called: the
    supervisor records and starts the new actor after building it, so a test that
    stopped waiting at the build read the spec while the restart was still on its
    way, and failed on a loaded machine.
    """
    actor = system.supervisor._specs[name].actor
    return actor if actor is not None and actor.state == ActorState.RUNNING else None


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02
) -> bool:
    """Poll until `predicate()` is true. Returns False on timeout.

    Preferred over a fixed `asyncio.sleep`: the test ends as soon as the
    supervisor has acted, and a slow machine gets more time rather than a
    spurious failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def make_system(poll_interval: float = 0.05) -> ActorSystem:
    """Create a minimal ActorSystem with a no-op MQTT client.
    poll_interval sets how fast the supervisor watch loop fires (default 50ms for tests).
    """

    system = ActorSystem()

    # Inject a no-op MQTT so actors don't need a broker
    class _NoOpMQTT:
        async def publish(self, t: str, p: dict[str, Any]) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    system._mqtt_client = _NoOpMQTT()  # pyright: ignore[reportAttributeAccessIssue]
    # Override supervisor with fast poll_interval
    system._supervisor = Supervisor(system.registry, system._inject, poll_interval=poll_interval)
    return system


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_stable_actor_not_restarted() -> None:
    """A healthy actor should never be restarted."""

    system = make_system()
    actor_ref = {}

    def factory() -> StableActor:
        a = StableActor(persistence_dir=STATE_DIR)
        actor_ref["a"] = a
        return a

    system.supervisor.supervise(
        "stable",
        factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=5,
    )
    await system.supervisor.start()

    # A stable actor must NOT be restarted, so there is no event to wait for —
    # give the watch loop several real chances to misbehave instead.
    await asyncio.sleep(system.supervisor._poll_interval * 4)

    actor = actor_ref["a"]
    assert_eq("actor state is RUNNING", actor.state, ActorState.RUNNING)
    assert_eq("started exactly once", actor.started_count, 1)
    assert_eq("restart_count is 0", actor.metrics.restart_count, 0)

    await system.supervisor.stop()


async def test_one_for_one_restart() -> None:
    """Crashed actor is restarted; siblings are untouched."""

    system = make_system()
    crash_ref = {}
    stable_ref = {}
    call_n = {"crash": 0}

    def crash_factory() -> CrashOnceActor:
        call_n["crash"] += 1
        # Only the FIRST instance should crash; subsequent restarts get healthy actor
        should_crash = call_n["crash"] == 1
        a = CrashOnceActor(should_crash=should_crash, persistence_dir=STATE_DIR)
        crash_ref["a"] = a
        return a

    def stable_factory() -> StableActor:
        a = StableActor(name="sibling", persistence_dir=STATE_DIR)
        stable_ref["a"] = a
        return a

    (
        system.supervisor.supervise(
            "crash-once",
            crash_factory,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=3,
            restart_delay=0.0,
        ).supervise(
            "sibling",
            stable_factory,
            strategy=SupervisorStrategy.ONE_FOR_ONE,
            max_restarts=3,
            restart_delay=0.0,
        )
    )
    await system.supervisor.start()

    assert await wait_until(
        lambda: call_n["crash"] >= 2 and running_actor(system, "crash-once") is not None
    )

    new_crash_actor = system.supervisor._specs["crash-once"].actor
    assert new_crash_actor
    sibling = stable_ref["a"]

    # The factory was called more than once — a new instance replaced the original
    assert_true("crash-once was restarted (factory called >1 time)", call_n["crash"] >= 2)
    assert_eq("crash-once is now RUNNING", new_crash_actor.state, ActorState.RUNNING)
    assert_eq("sibling still same instance", sibling is stable_ref["a"], True)
    assert_eq("sibling still RUNNING", sibling.state, ActorState.RUNNING)
    assert_eq("sibling started only once", sibling.started_count, 1)

    await system.supervisor.stop()


async def test_restart_count_increments() -> None:
    """restart_count on ActorMetrics increments after each supervisor restart."""

    system = make_system()

    # Build an actor that crashes the first 2 times, then is healthy
    crash_counter = {"n": 0}

    def factory() -> CrashOnceActor:
        crash_counter["n"] += 1
        should_crash = crash_counter["n"] <= 2
        return CrashOnceActor(name="counted", should_crash=should_crash, persistence_dir=STATE_DIR)

    system.supervisor.supervise(
        "counted",
        factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=5,
        restart_delay=0.0,
    )
    await system.supervisor.start()

    # The initial start and two restarts, the last one finished.
    assert await wait_until(
        lambda: crash_counter["n"] >= 3 and running_actor(system, "counted") is not None
    )

    final = system.supervisor._specs["counted"].actor
    assert final
    assert_true(
        "restart_count >= 2", final.metrics.restart_count >= 2, f"got {final.metrics.restart_count}"
    )
    assert_eq("actor is now RUNNING", final.state, ActorState.RUNNING)

    await system.supervisor.stop()


async def test_repeated_crashes_slow_restarts_down() -> None:
    """After max_restarts crashes in a row the supervisor slows down, and keeps it."""

    system = make_system()
    notifications = []

    # Inject a fake main that collects notifications
    class FakeMain:
        name = "main"
        _pending_notifications = notifications
        actor_id = "fake-main-id"

    system.registry._actors["fake-main-id"] = FakeMain()  # pyright: ignore[reportArgumentType]
    # Patch find_by_name to return FakeMain
    original_find = system.registry.find_by_name
    system.registry.find_by_name = lambda n: FakeMain() if n == "main" else original_find(n)  # pyright: ignore[reportAttributeAccessIssue]

    start_count = {"n": 0}

    def factory() -> Actor:
        a = AlwaysCrashActor(persistence_dir=STATE_DIR)
        start_count["n"] += 1
        return a

    system.supervisor.supervise(
        "always-crash",
        factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=3,
        restart_window=30.0,
        restart_delay=0.0,
    )
    await system.supervisor.start()

    await wait_until(lambda: system.supervisor._specs["always-crash"].slow)

    spec = system.supervisor._specs["always-crash"]
    assert_true("restarts slowed down", spec.slow, f"crash_streak={spec.crash_streak}")
    assert_true("still supervised", not spec.retired)
    # The initial start plus max_restarts quick restarts; the next waits minutes.
    assert_true(
        "supervisor stopped restarting quickly (start_count <= 4)",
        start_count["n"] <= 4,
        f"start_count={start_count['n']}",
    )

    await system.supervisor.stop()


async def test_one_for_all_restarts_siblings() -> None:
    """ONE_FOR_ALL: crashing one restarts all supervised actors."""

    system = make_system()
    start_counts = {"alpha": 0, "beta": 0, "gamma": 0}

    def make_factory(name: str, should_crash: bool) -> Callable[[], CrashOnceActor]:
        call_n = {"n": 0}

        def factory() -> CrashOnceActor:
            call_n["n"] += 1
            start_counts[name] += 1
            crash_this_time = should_crash and call_n["n"] == 1
            return CrashOnceActor(
                name=name, should_crash=crash_this_time, persistence_dir=STATE_DIR
            )

        return factory

    (
        system.supervisor.supervise(
            "alpha",
            make_factory("alpha", should_crash=True),
            strategy=SupervisorStrategy.ONE_FOR_ALL,
            max_restarts=3,
            restart_delay=0.0,
        )
        .supervise(
            "beta",
            make_factory("beta", should_crash=False),
            strategy=SupervisorStrategy.ONE_FOR_ALL,
            max_restarts=3,
            restart_delay=0.0,
        )
        .supervise(
            "gamma",
            make_factory("gamma", should_crash=False),
            strategy=SupervisorStrategy.ONE_FOR_ALL,
            max_restarts=3,
            restart_delay=0.0,
        )
    )
    await system.supervisor.start()

    await wait_until(lambda: min(start_counts.values()) >= 2)

    # All three should have been started more than once
    assert_true(
        "alpha restarted", start_counts["alpha"] >= 2, f"alpha starts={start_counts['alpha']}"
    )
    assert_true(
        "beta restarted by ONE_FOR_ALL",
        start_counts["beta"] >= 2,
        f"beta starts={start_counts['beta']}",
    )
    assert_true(
        "gamma restarted by ONE_FOR_ALL",
        start_counts["gamma"] >= 2,
        f"gamma starts={start_counts['gamma']}",
    )

    await system.supervisor.stop()


async def test_rest_for_one_only_downstream() -> None:
    """REST_FOR_ONE: only the crashed actor and those after it restart."""

    system = make_system()
    start_counts = {"upstream": 0, "middle": 0, "downstream": 0}

    def make_factory(name: str, should_crash: bool) -> Callable[[], CrashOnceActor]:
        call_n = {"n": 0}

        def factory() -> CrashOnceActor:
            call_n["n"] += 1
            start_counts[name] += 1
            crash_this_time = should_crash and call_n["n"] == 1
            return CrashOnceActor(
                name=name, should_crash=crash_this_time, persistence_dir=STATE_DIR
            )

        return factory

    (
        system.supervisor
        # upstream registered first — should NOT be restarted
        .supervise(
            "upstream",
            make_factory("upstream", should_crash=False),
            strategy=SupervisorStrategy.REST_FOR_ONE,
            max_restarts=3,
            restart_delay=0.0,
        )
        # middle crashes — it and downstream should restart
        .supervise(
            "middle",
            make_factory("middle", should_crash=True),
            strategy=SupervisorStrategy.REST_FOR_ONE,
            max_restarts=3,
            restart_delay=0.0,
        )
        .supervise(
            "downstream",
            make_factory("downstream", should_crash=False),
            strategy=SupervisorStrategy.REST_FOR_ONE,
            max_restarts=3,
            restart_delay=0.0,
        )
    )
    await system.supervisor.start()

    await wait_until(lambda: start_counts["middle"] >= 2 and start_counts["downstream"] >= 2)

    assert_eq("upstream NOT restarted (started once)", start_counts["upstream"], 1)
    assert_true(
        "middle restarted", start_counts["middle"] >= 2, f"middle starts={start_counts['middle']}"
    )
    assert_true(
        "downstream restarted by REST_FOR_ONE",
        start_counts["downstream"] >= 2,
        f"downstream starts={start_counts['downstream']}",
    )

    await system.supervisor.stop()


async def test_supervisor_status_snapshot() -> None:
    """supervisor.status() returns correct info for all registered actors."""

    system = make_system()

    def factory() -> Actor:
        return StableActor(name="snap", persistence_dir=STATE_DIR)

    system.supervisor.supervise(
        "snap",
        factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=7,
    )
    await system.supervisor.start()
    await asyncio.sleep(0.1)

    snapshot = system.supervisor.status()
    assert_eq("one entry in snapshot", len(snapshot), 1)
    entry = snapshot[0]
    assert_eq("name correct", entry["name"], "snap")
    assert_eq("strategy correct", entry["strategy"], "one_for_one")
    assert_eq("max_restarts correct", entry["max_restarts"], 7)
    assert_eq("actor_state is running", entry["actor_state"], "running")
    assert_eq("not slowed down", entry["slow_retry"], False)
    assert_eq("no crash streak", entry["crash_streak"], 0)

    await system.supervisor.stop()


async def test_supervised_flag_on_actor() -> None:
    """Actor.get_status() reports supervised=True when under a supervisor."""

    system = make_system()

    def factory() -> Actor:
        return StableActor(name="flag-test", persistence_dir=STATE_DIR)

    system.supervisor.supervise(
        "flag-test", factory, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=3
    )
    await system.supervisor.start()
    await asyncio.sleep(0.1)

    actor = system.supervisor._specs["flag-test"].actor
    assert actor
    status = actor.get_status()
    assert_eq("supervised=True in get_status", status.get("supervised"), True)
    assert_eq("restart_count=0 in get_status", status.get("restart_count"), 0)

    await system.supervisor.stop()


async def test_stop_all_stops_supervisor() -> None:
    """ActorSystem.stop_all() shuts down the supervisor watch loop."""

    system = make_system()

    def factory() -> Actor:
        return StableActor(name="teardown", persistence_dir=STATE_DIR)

    system.supervisor.supervise(
        "teardown", factory, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=3
    )
    await system.supervisor.start()
    await asyncio.sleep(0.05)

    watch_task = system.supervisor._watch_task
    assert_true("watch task is running before stop_all", watch_task and not watch_task.done())

    await system.stop_all()
    # Give the event loop one tick to process the cancellation
    await asyncio.sleep(0)

    assert_true("watch task is cancelled after stop_all", watch_task and watch_task.done())
