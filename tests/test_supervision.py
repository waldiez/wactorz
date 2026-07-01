"""
test_supervision.py — Wactorz Supervisor stress test
=====================================================

Tests all three crash-detection modes added in the supervision overhaul.
Needs NO MQTT broker, NO LLM, NO database.  Run with:

    python test_supervision.py

Expected output (in order):
    [PASS] Test 1 — FAILED state detected and restarted
    [PASS] Test 2 — Error storm detected and restarted
    [PASS] Test 3 — Heartbeat silence detected and restarted
    [PASS] Test 4 — Intentional stop NOT restarted
    [PASS] Test 5 — Intentional delete NOT restarted
    [PASS] Test 6 — Budget exhaustion retires the spec (no infinite loop)

Each test is isolated and prints a clear PASS / FAIL line.
"""

import asyncio
import logging
import sys
import time

# ── Silence framework noise so test output is readable ──────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s  %(message)s",
)
# But keep Supervisor messages so we can see it working
logging.getLogger("wactorz.core.registry").setLevel(logging.DEBUG)

from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.registry import ActorRegistry, Supervisor

# ── Helpers ──────────────────────────────────────────────────────────────────


def make_registry() -> ActorRegistry:
    reg = ActorRegistry()
    return reg


def make_supervisor(registry: ActorRegistry) -> Supervisor:
    def noop_inject(actor):
        pass  # no MQTT in tests

    sup = Supervisor(registry, noop_inject, poll_interval=0.3)
    registry._supervisor_ref = sup
    return sup


PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"


def result(label: str, ok: bool):
    tag = PASS if ok else FAIL
    print(f"  {tag}  {label}")
    if not ok:
        # Keep running remaining tests but flag overall failure
        result._any_failed = True


result._any_failed = False


# ── Minimal actor base that skips MQTT entirely ───────────────────────────────


class _TestActor(Actor):
    """Bare-bones actor for testing — no MQTT, no persistence."""

    def __init__(self, name: str, **kwargs):
        super().__init__(name=name, **kwargs)
        self.spawn_count = 0  # incremented by factory each time a fresh instance is made

    async def _mqtt_publish(self, topic, payload, retain=False, qos=0):
        pass  # swallow — no broker in tests

    async def _command_listener(self):
        pass  # no MQTT → no command listener

    async def _save_persistent_state(self):
        pass

    async def _load_persistent_state(self):
        pass

    async def handle_message(self, msg: Message):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — FAILED state triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test1_failed_state():
    print("\nTest 1 — FAILED state detected and restarted")

    spawn_count = {"n": 0}

    class CrashyActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # First spawn: immediately mark ourselves FAILED
                self.state = ActorState.FAILED

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory():
        return CrashyActor(name="crashy-1")

    sup.supervise("crashy-1", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    # Give the watch loop time to detect and restart
    await asyncio.sleep(1.5)
    await sup.stop()

    # Should have spawned at least twice (initial + 1 restart)
    result("Actor restarted after FAILED state", spawn_count["n"] >= 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Error storm triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test2_error_storm():
    print("\nTest 2 — Error storm detected and restarted")

    spawn_count = {"n": 0}

    class StormyActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # Immediately pile on errors above ERROR_STORM_THRESHOLD (10)
                self.metrics.errors = 15

    registry = make_registry()
    sup = make_supervisor(registry)
    sup.ERROR_STORM_THRESHOLD = 10  # explicit for clarity

    def factory():
        return StormyActor(name="stormy-2")

    sup.supervise("stormy-2", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    await asyncio.sleep(1.5)
    await sup.stop()

    result("Actor restarted after error storm", spawn_count["n"] >= 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Heartbeat silence triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test3_heartbeat_silence():
    print("\nTest 3 — Heartbeat silence detected and restarted")

    spawn_count = {"n": 0}

    class SilentActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # Pretend the actor started a long time ago and last heartbeat was ancient
                # Use a large negative offset to fake old uptime
                self.metrics.start_time = time.time() - 200  # 200s ago
                self.metrics.last_heartbeat = time.time() - 100  # 100s ago

        async def _heartbeat_loop(self, interval=10.0):
            pass  # silence the heartbeat loop so last_heartbeat stays stale

    registry = make_registry()
    sup = make_supervisor(registry)
    sup.HEARTBEAT_TIMEOUT = 35.0  # keep default

    def factory():
        return SilentActor(name="silent-3")

    sup.supervise("silent-3", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    await asyncio.sleep(1.5)
    await sup.stop()

    result("Actor restarted after heartbeat silence", spawn_count["n"] >= 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — Intentional stop NOT restarted
# ═══════════════════════════════════════════════════════════════════════════════


async def test4_intentional_stop():
    print("\nTest 4 — Intentional stop NOT restarted")

    spawn_count = {"n": 0}

    class NormalActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory():
        return NormalActor(name="normal-4")

    sup.supervise("normal-4", factory, max_restarts=5, restart_delay=0.0)
    await sup.start()

    # Let it settle
    await asyncio.sleep(0.3)

    # Intentional stop — call release() then stop()
    spec = sup._specs.get("normal-4")
    actor = spec.actor if spec else None
    if actor:
        sup.release("normal-4")  # unlink from supervision
        await actor.stop()

    # Wait long enough that the watch loop runs multiple cycles
    await asyncio.sleep(1.5)
    await sup.stop()

    # Should have spawned exactly once
    result("Actor NOT restarted after intentional stop", spawn_count["n"] == 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — Intentional delete NOT restarted
# ═══════════════════════════════════════════════════════════════════════════════


async def test5_intentional_delete():
    print("\nTest 5 — Intentional delete NOT restarted")

    spawn_count = {"n": 0}

    class NormalActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory():
        return NormalActor(name="normal-5")

    sup.supervise("normal-5", factory, max_restarts=5, restart_delay=0.0)
    await sup.start()

    await asyncio.sleep(0.3)

    spec = sup._specs.get("normal-5")
    actor = spec.actor if spec else None
    if actor:
        sup.release("normal-5")  # unlink
        await registry.unregister(actor.actor_id)
        await actor.stop()

    await asyncio.sleep(1.5)
    await sup.stop()

    result("Actor NOT restarted after intentional delete", spawn_count["n"] == 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — Budget exhaustion retires the spec (no infinite loop)
# ═══════════════════════════════════════════════════════════════════════════════


async def test6_budget_exhaustion():
    print("\nTest 6 — Budget exhaustion retires spec (no infinite loop)")

    spawn_count = {"n": 0}

    class AlwaysCrashActor(_TestActor):
        async def on_start(self):
            spawn_count["n"] += 1
            self.state = ActorState.FAILED  # always crash immediately

    registry = make_registry()
    sup = make_supervisor(registry)

    MAX = 3

    def factory():
        return AlwaysCrashActor(name="always-crash-6")

    sup.supervise(
        "always-crash-6",
        factory,
        max_restarts=MAX,
        restart_window=60.0,
        restart_delay=0.0,
    )
    await sup.start()

    # Give it enough time to exhaust the budget
    await asyncio.sleep(3.0)

    spec = sup._specs.get("always-crash-6")
    retired = spec.retired if spec else False

    # After budget is gone, watch loop must not keep calling restart
    count_at_retirement = spawn_count["n"]
    await asyncio.sleep(1.0)  # one more second — count must not grow
    count_after_pause = spawn_count["n"]

    await sup.stop()

    result(
        f"Spec retired after {MAX} restarts (spawned={count_at_retirement})",
        retired and count_at_retirement <= MAX + 1,  # initial + MAX restarts
    )
    result(
        "No further spawns after retirement",
        count_after_pause == count_at_retirement,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════


async def main():
    print("=" * 60)
    print("  Wactorz Supervisor Test Suite")
    print("=" * 60)

    await test1_failed_state()
    await test2_error_storm()
    await test3_heartbeat_silence()
    await test4_intentional_stop()
    await test5_intentional_delete()
    await test6_budget_exhaustion()

    print()
    if result._any_failed:
        print("  ❌  Some tests FAILED — see above.")
        sys.exit(1)
    else:
        print("  ✅  All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
