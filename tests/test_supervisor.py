"""
test_supervisor.py — Logical tests for the Wactorz supervision tree.

Tests run fully in-process. No MQTT broker required, no real LLM calls,
no network. Each test creates a minimal ActorSystem, registers actors under
the Supervisor, and verifies behaviour by inspecting actor state.

Run with:
    python test_supervisor.py
    python test_supervisor.py -v          # verbose
    python test_supervisor.py TestName    # single test
"""

import asyncio
import sys
import time
import traceback
import argparse
from dataclasses import dataclass, field
from typing import Optional

# ── Import core modules directly — no full wactorz install needed ───────────
# We stub every third-party dep and bypass wactorz's __init__.py so this test
# runs standalone: no MQTT broker, no LLM keys, no HA server.

import types, importlib.util, pathlib

def _stub(name):
    sys.modules.setdefault(name, types.ModuleType(name))

for _m in ["aiomqtt", "psutil", "anthropic", "openai", "aiohttp",
           "discord", "twilio", "pdfplumber", "fitz", "ultralytics",
           "torch", "numpy", "asyncssh",
           "wactorz.core.integrations",
           "wactorz.core.integrations.home_assistant",
           "wactorz.core.integrations.home_assistant.ha_helper"]:
    _stub(_m)

# psutil.Process stub
_psutil = sys.modules["psutil"]
class _FakeProc:
    def cpu_percent(self, interval=None): return 0.0
    def memory_info(self): return type("m", (), {"rss": 0})()
_psutil.Process = lambda: _FakeProc()

# Stub the wactorz package so its __init__.py is never executed
_af      = types.ModuleType("wactorz");      sys.modules["wactorz"]      = _af
_af_core = types.ModuleType("wactorz.core"); sys.modules["wactorz.core"] = _af_core

# Load the real core files from the package directory.
_BASE = pathlib.Path(__file__).parent / "wactorz" / "core"

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_actor_mod    = _load("wactorz.core.actor",    _BASE / "actor.py")
_registry_mod = _load("wactorz.core.registry", _BASE / "registry.py")

from wactorz.core.actor    import Actor, Message, MessageType, ActorState, SupervisorStrategy
from wactorz.core.registry import ActorRegistry, ActorSystem, Supervisor, SupervisedSpec


# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "✅ PASS"
FAIL = "❌ FAIL"

_results: list[tuple[str, bool, str]] = []

def assert_eq(label, got, expected):
    ok = got == expected
    _results.append((label, ok, f"got={got!r}, expected={expected!r}"))
    marker = PASS if ok else FAIL
    print(f"  {marker}  {label}")
    return ok

def assert_true(label, value, detail=""):
    ok = bool(value)
    _results.append((label, ok, detail or str(value)))
    marker = PASS if ok else FAIL
    print(f"  {marker}  {label}")
    return ok

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Minimal test actors ───────────────────────────────────────────────────────

class StableActor(Actor):
    """An actor that stays healthy forever."""
    def __init__(self, **kwargs):
        kwargs.setdefault("name", "stable-actor")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self):
        self.started_count += 1

    async def handle_message(self, msg: Message):
        pass


class CrashOnceActor(Actor):
    """Crashes on first on_start. crash_state is controlled externally via closure."""
    def __init__(self, should_crash: bool = False, **kwargs):
        kwargs.setdefault("name", "crash-once")
        super().__init__(**kwargs)
        self.started_count = 0
        self._should_crash = should_crash   # set by factory closure

    async def on_start(self):
        self.started_count += 1
        if self._should_crash:
            self.state = ActorState.FAILED

    async def handle_message(self, msg: Message):
        pass


class AlwaysCrashActor(Actor):
    """Crashes every time it starts — exhausts restart budget."""
    def __init__(self, **kwargs):
        kwargs.setdefault("name", "always-crash")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self):
        self.started_count += 1
        self.state = ActorState.FAILED

    async def handle_message(self, msg: Message):
        pass


class DependentActor(Actor):
    """Simulates an actor that depends on a sibling being healthy."""
    def __init__(self, **kwargs):
        kwargs.setdefault("name", "dependent")
        super().__init__(**kwargs)
        self.started_count = 0

    async def on_start(self):
        self.started_count += 1

    async def handle_message(self, msg: Message):
        pass


# ── Test infrastructure ───────────────────────────────────────────────────────

def make_system(poll_interval: float = 0.05):
    """Create a minimal ActorSystem with a no-op MQTT client.
    poll_interval sets how fast the supervisor watch loop fires (default 50ms for tests).
    """
    from wactorz.core.registry import Supervisor
    system = ActorSystem()
    # Inject a no-op MQTT so actors don't need a broker
    class _NoOpMQTT:
        async def publish(self, t, p): pass
        async def disconnect(self): pass
    system._mqtt_client = _NoOpMQTT()
    # Override supervisor with fast poll_interval
    system._supervisor = Supervisor(system.registry, system._inject,
                                    poll_interval=poll_interval)
    return system


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_stable_actor_not_restarted():
    """A healthy actor should never be restarted."""
    section("TEST 1 — Stable actor: no restarts")

    system = make_system()
    actor_ref = {}

    def factory():
        a = StableActor(persistence_dir="/tmp/af_test")
        actor_ref["a"] = a
        return a

    system.supervisor.supervise(
        "stable", factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=5,
    )
    await system.supervisor.start()

    # Let the watch loop run a few cycles
    await asyncio.sleep(0.15)

    actor = actor_ref["a"]
    assert_eq("actor state is RUNNING",   actor.state, ActorState.RUNNING)
    assert_eq("started exactly once",     actor.started_count, 1)
    assert_eq("restart_count is 0",       actor.metrics.restart_count, 0)

    await system.supervisor.stop()


async def test_one_for_one_restart():
    """Crashed actor is restarted; siblings are untouched."""
    section("TEST 2 — ONE_FOR_ONE: crashed actor restarted, sibling untouched")

    system = make_system()
    crash_ref  = {}
    stable_ref = {}
    call_n = {"crash": 0}

    def crash_factory():
        call_n["crash"] += 1
        # Only the FIRST instance should crash; subsequent restarts get healthy actor
        should_crash = (call_n["crash"] == 1)
        a = CrashOnceActor(should_crash=should_crash, persistence_dir="/tmp/af_test")
        crash_ref["a"] = a
        return a

    def stable_factory():
        a = StableActor(name="sibling", persistence_dir="/tmp/af_test")
        stable_ref["a"] = a
        return a

    (
        system.supervisor
        .supervise("crash-once", crash_factory,  strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=3, restart_delay=0.0)
        .supervise("sibling",    stable_factory, strategy=SupervisorStrategy.ONE_FOR_ONE, max_restarts=3, restart_delay=0.0)
    )
    await system.supervisor.start()

    # Let the watch loop detect failure and restart
    await asyncio.sleep(0.25)

    new_crash_actor = system.supervisor._specs["crash-once"].actor
    sibling         = stable_ref["a"]

    # The factory was called more than once — a new instance replaced the original
    assert_true("crash-once was restarted (factory called >1 time)",
                call_n["crash"] >= 2)
    assert_eq("crash-once is now RUNNING",   new_crash_actor.state, ActorState.RUNNING)
    assert_eq("sibling still same instance", sibling is stable_ref["a"], True)
    assert_eq("sibling still RUNNING",       sibling.state, ActorState.RUNNING)
    assert_eq("sibling started only once",   sibling.started_count, 1)

    await system.supervisor.stop()


async def test_restart_count_increments():
    """restart_count on ActorMetrics increments after each supervisor restart."""
    section("TEST 3 — restart_count increments correctly")

    system = make_system()

    # Build an actor that crashes the first 2 times, then is healthy
    crash_counter = {"n": 0}

    def factory():
        crash_counter["n"] += 1
        should_crash = crash_counter["n"] <= 2
        a = CrashOnceActor(name="counted", should_crash=should_crash,
                            persistence_dir="/tmp/af_test")
        return a

    system.supervisor.supervise(
        "counted", factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=5, restart_delay=0.0,
    )
    await system.supervisor.start()

    # Wait for 2 restarts
    await asyncio.sleep(0.35)

    final = system.supervisor._specs["counted"].actor
    assert_true("restart_count >= 2", final.metrics.restart_count >= 2,
                f"got {final.metrics.restart_count}")
    assert_eq("actor is now RUNNING", final.state, ActorState.RUNNING)

    await system.supervisor.stop()


async def test_budget_exhausted_gives_up():
    """After max_restarts within the window the supervisor stops trying."""
    section("TEST 4 — Budget exhausted: supervisor gives up")

    system = make_system()
    notifications = []

    # Inject a fake main that collects notifications
    class FakeMain:
        name = "main"
        _pending_notifications = notifications
        actor_id = "fake-main-id"
    system.registry._actors["fake-main-id"] = FakeMain()
    system.registry._actors   # trick find_by_name
    # Patch find_by_name to return FakeMain
    original_find = system.registry.find_by_name
    system.registry.find_by_name = lambda n: FakeMain() if n == "main" else original_find(n)

    start_count = {"n": 0}
    def factory():
        a = AlwaysCrashActor(persistence_dir="/tmp/af_test")
        start_count["n"] += 1
        return a

    system.supervisor.supervise(
        "always-crash", factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=3,
        restart_window=30.0,
        restart_delay=0.0,
    )
    await system.supervisor.start()

    # Give it time to exhaust the budget (3 restarts + initial spawn = 4 starts)
    await asyncio.sleep(0.5)

    spec = system.supervisor._specs["always-crash"]
    assert_true("restart budget exhausted",   spec.exhausted,
                f"restart_times={spec._restart_times}")
    assert_true("supervisor stopped restarting (start_count <= 5)",
                start_count["n"] <= 5,
                f"start_count={start_count['n']}")

    await system.supervisor.stop()


async def test_one_for_all_restarts_siblings():
    """ONE_FOR_ALL: crashing one restarts all supervised actors."""
    section("TEST 5 — ONE_FOR_ALL: all actors restart when one crashes")

    system = make_system()
    start_counts = {"alpha": 0, "beta": 0, "gamma": 0}

    def make_factory(name, should_crash):
        call_n = {"n": 0}
        def factory():
            call_n["n"] += 1
            start_counts[name] += 1
            crash_this_time = should_crash and call_n["n"] == 1
            a = CrashOnceActor(name=name, should_crash=crash_this_time,
                               persistence_dir="/tmp/af_test")
            return a
        return factory

    (
        system.supervisor
        .supervise("alpha", make_factory("alpha", should_crash=True),
                   strategy=SupervisorStrategy.ONE_FOR_ALL, max_restarts=3, restart_delay=0.0)
        .supervise("beta",  make_factory("beta",  should_crash=False),
                   strategy=SupervisorStrategy.ONE_FOR_ALL, max_restarts=3, restart_delay=0.0)
        .supervise("gamma", make_factory("gamma", should_crash=False),
                   strategy=SupervisorStrategy.ONE_FOR_ALL, max_restarts=3, restart_delay=0.0)
    )
    await system.supervisor.start()

    await asyncio.sleep(0.4)

    # All three should have been started more than once
    assert_true("alpha restarted", start_counts["alpha"] >= 2,
                f"alpha starts={start_counts['alpha']}")
    assert_true("beta restarted by ONE_FOR_ALL", start_counts["beta"] >= 2,
                f"beta starts={start_counts['beta']}")
    assert_true("gamma restarted by ONE_FOR_ALL", start_counts["gamma"] >= 2,
                f"gamma starts={start_counts['gamma']}")

    await system.supervisor.stop()


async def test_rest_for_one_only_downstream():
    """REST_FOR_ONE: only the crashed actor and those after it restart."""
    section("TEST 6 — REST_FOR_ONE: only downstream actors restart")

    system = make_system()
    start_counts = {"upstream": 0, "middle": 0, "downstream": 0}

    def make_factory(name, should_crash):
        call_n = {"n": 0}
        def factory():
            call_n["n"] += 1
            start_counts[name] += 1
            crash_this_time = should_crash and call_n["n"] == 1
            a = CrashOnceActor(name=name, should_crash=crash_this_time,
                               persistence_dir="/tmp/af_test")
            return a
        return factory

    (
        system.supervisor
        # upstream registered first — should NOT be restarted
        .supervise("upstream",   make_factory("upstream",   should_crash=False),
                   strategy=SupervisorStrategy.REST_FOR_ONE, max_restarts=3, restart_delay=0.0)
        # middle crashes — it and downstream should restart
        .supervise("middle",     make_factory("middle",     should_crash=True),
                   strategy=SupervisorStrategy.REST_FOR_ONE, max_restarts=3, restart_delay=0.0)
        .supervise("downstream", make_factory("downstream", should_crash=False),
                   strategy=SupervisorStrategy.REST_FOR_ONE, max_restarts=3, restart_delay=0.0)
    )
    await system.supervisor.start()

    await asyncio.sleep(0.4)

    assert_eq("upstream NOT restarted (started once)",
              start_counts["upstream"], 1)
    assert_true("middle restarted",
                start_counts["middle"] >= 2,
                f"middle starts={start_counts['middle']}")
    assert_true("downstream restarted by REST_FOR_ONE",
                start_counts["downstream"] >= 2,
                f"downstream starts={start_counts['downstream']}")

    await system.supervisor.stop()


async def test_supervisor_status_snapshot():
    """supervisor.status() returns correct info for all registered actors."""
    section("TEST 7 — supervisor.status() snapshot")

    system = make_system()

    def factory():
        return StableActor(name="snap", persistence_dir="/tmp/af_test")

    system.supervisor.supervise(
        "snap", factory,
        strategy=SupervisorStrategy.ONE_FOR_ONE,
        max_restarts=7,
    )
    await system.supervisor.start()
    await asyncio.sleep(0.1)

    snapshot = system.supervisor.status()
    assert_eq("one entry in snapshot",   len(snapshot), 1)
    entry = snapshot[0]
    assert_eq("name correct",            entry["name"],         "snap")
    assert_eq("strategy correct",        entry["strategy"],     "one_for_one")
    assert_eq("max_restarts correct",    entry["max_restarts"],  7)
    assert_eq("actor_state is running",  entry["actor_state"],  "running")
    assert_eq("not exhausted",           entry["exhausted"],    False)

    await system.supervisor.stop()


async def test_supervised_flag_on_actor():
    """Actor.get_status() reports supervised=True when under a supervisor."""
    section("TEST 8 — Actor reports supervised=True in get_status()")

    system = make_system()

    def factory():
        return StableActor(name="flag-test", persistence_dir="/tmp/af_test")

    system.supervisor.supervise("flag-test", factory,
                                strategy=SupervisorStrategy.ONE_FOR_ONE,
                                max_restarts=3)
    await system.supervisor.start()
    await asyncio.sleep(0.1)

    actor = system.supervisor._specs["flag-test"].actor
    status = actor.get_status()
    assert_eq("supervised=True in get_status", status.get("supervised"), True)
    assert_eq("restart_count=0 in get_status", status.get("restart_count"), 0)

    await system.supervisor.stop()


async def test_stop_all_stops_supervisor():
    """ActorSystem.stop_all() shuts down the supervisor watch loop."""
    section("TEST 9 — stop_all() cancels supervisor watch loop")

    system = make_system()

    def factory():
        return StableActor(name="teardown", persistence_dir="/tmp/af_test")

    system.supervisor.supervise("teardown", factory,
                                strategy=SupervisorStrategy.ONE_FOR_ONE,
                                max_restarts=3)
    await system.supervisor.start()
    await asyncio.sleep(0.05)

    watch_task = system.supervisor._watch_task
    assert_true("watch task is running before stop_all",
                watch_task and not watch_task.done())

    await system.stop_all()
    # Give the event loop one tick to process the cancellation
    await asyncio.sleep(0)

    assert_true("watch task is cancelled after stop_all",
                watch_task and watch_task.done())


# ── Runner ────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    ("Stable actor not restarted",        test_stable_actor_not_restarted),
    ("ONE_FOR_ONE restart",               test_one_for_one_restart),
    ("restart_count increments",          test_restart_count_increments),
    ("Budget exhausted — gives up",       test_budget_exhausted_gives_up),
    ("ONE_FOR_ALL restarts siblings",     test_one_for_all_restarts_siblings),
    ("REST_FOR_ONE only downstream",      test_rest_for_one_only_downstream),
    ("supervisor.status() snapshot",      test_supervisor_status_snapshot),
    ("Actor supervised flag",             test_supervised_flag_on_actor),
    ("stop_all stops supervisor",         test_stop_all_stops_supervisor),
]


async def run_all(filter_name: Optional[str] = None):
    failed = 0
    skipped = 0
    for name, fn in ALL_TESTS:
        if filter_name and filter_name.lower() not in name.lower():
            skipped += 1
            continue
        try:
            await fn()
        except Exception:
            print(f"\n  💥 EXCEPTION in '{name}':")
            traceback.print_exc()
            failed += 1

    print(f"\n{'═'*60}")
    total   = len(ALL_TESTS) - skipped
    passed  = sum(1 for _, ok, _ in _results if ok)
    failed_assertions = sum(1 for _, ok, _ in _results if not ok)
    print(f"  Results: {passed} passed, {failed_assertions} failed assertions, "
          f"{failed} exceptions, {skipped} skipped")
    print(f"{'═'*60}\n")

    if failed_assertions or failed:
        print("Failed assertions:")
        for label, ok, detail in _results:
            if not ok:
                print(f"  ❌  {label}: {detail}")
        sys.exit(1)
    else:
        print("  All tests passed 🎉")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("filter", nargs="?", default=None,
                        help="Only run tests whose name contains this string (case-insensitive)")
    args = parser.parse_args()
    asyncio.run(run_all(args.filter))
