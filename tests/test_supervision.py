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
    [PASS] Test 5b — release() unlinks even a FAILED actor
    [PASS] Test 6 — Budget exhaustion retires the spec (no infinite loop)

Each test is isolated and prints a clear PASS / FAIL line.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"

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
    return ActorRegistry()


def make_supervisor(registry: ActorRegistry, poll_interval: float = 0.05) -> Supervisor:
    def noop_inject(actor: Actor) -> None:
        pass  # no MQTT in tests

    sup = Supervisor(registry, noop_inject, poll_interval=poll_interval)
    registry._supervisor_ref = sup
    return sup


def spec_state(sup: Supervisor, name: str) -> Any:
    """Current state of the actor a spec holds, or None if it holds nothing."""
    spec = sup._specs.get(name)
    return getattr(getattr(spec, "actor", None), "state", None)


def errors_of(sup: Supervisor, name: str) -> int:
    """Error counter of the actor a spec currently holds."""
    actor = getattr(sup._specs.get(name), "actor", None)
    return getattr(getattr(actor, "metrics", None), "errors", 0)


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02
) -> bool:
    """Poll until `predicate()` is true. Returns False on timeout.

    Used instead of a fixed `asyncio.sleep` so a test finishes as soon as the
    supervisor has acted, rather than always paying the worst case — and so a
    slow machine widens the tolerance instead of going red.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def settle(sup: Supervisor, cycles: int = 4) -> None:
    """Let the watch loop run `cycles` times.

    For the negative tests only: you cannot wait *for* something that must never
    happen, so give the supervisor several real chances to misbehave.
    """
    await asyncio.sleep(sup._poll_interval * cycles)


def result(label: str, ok: bool) -> None:
    """Print the PASS/FAIL line *and* assert, so pytest can see a failure.

    Printing alone is why this file used to pass no matter what the supervisor
    did: `_any_failed` is only consulted by the __main__ runner below, which
    pytest never calls. Mirrors `assert_eq` in test_supervisor.py.
    """
    tag = PASS if ok else FAIL
    print(f"  {tag}  {label}")
    if not ok:
        # Keep the standalone runner's "report everything" behaviour (see main).
        result._any_failed = True
    assert ok, label


result._any_failed = False


# ── Minimal actor base that skips MQTT entirely ───────────────────────────────


class _TestActor(Actor):
    """Bare-bones actor for testing — no MQTT, no persistence."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(name=name, **kwargs)
        self.spawn_count = 0  # incremented by factory each time a fresh instance is made

    async def _mqtt_publish(
        self, topic: str, payload: dict[str, Any], retain: bool = False, qos: int = 0
    ) -> None:
        pass  # swallow — no broker in tests

    async def _command_listener(self) -> None:
        pass  # no MQTT → no command listener

    async def _save_persistent_state(self) -> None:
        pass

    async def _load_persistent_state(self) -> None:
        pass

    async def handle_message(self, msg: Message) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — FAILED state triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test1_failed_state() -> None:
    print("\nTest 1 — FAILED state detected and restarted")

    spawn_count = {"n": 0}

    class CrashyActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # First spawn: immediately mark ourselves FAILED
                self.state = ActorState.FAILED

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory() -> CrashyActor:
        return CrashyActor(name="crashy-1")

    sup.supervise("crashy-1", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    restarted = await wait_until(lambda: spawn_count["n"] >= 2)
    await sup.stop()

    # Initial spawn + at least one restart.
    result("Actor restarted after FAILED state", restarted)
    result(
        "Actor is running again after the restart", spec_state(sup, "crashy-1") != ActorState.FAILED
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Error storm triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test2_error_storm() -> None:
    print("\nTest 2 — Error storm detected and restarted")

    spawn_count = {"n": 0}

    class StormyActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # Immediately pile on errors above ERROR_STORM_THRESHOLD (10)
                self.metrics.errors = 15

    registry = make_registry()
    sup = make_supervisor(registry)
    sup.ERROR_STORM_THRESHOLD = 10  # explicit for clarity

    def factory() -> StormyActor:
        return StormyActor(name="stormy-2")

    sup.supervise("stormy-2", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    restarted = await wait_until(lambda: spawn_count["n"] >= 2)
    await sup.stop()

    result("Actor restarted after error storm", restarted)
    result("Error counter reset by the restart", errors_of(sup, "stormy-2") < 15)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — Heartbeat silence triggers restart
# ═══════════════════════════════════════════════════════════════════════════════


async def test3_heartbeat_silence() -> None:
    print("\nTest 3 — Heartbeat silence detected and restarted")

    spawn_count = {"n": 0}

    class SilentActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1
            if spawn_count["n"] == 1:
                # Pretend the actor started a long time ago and last heartbeat was ancient
                # Use a large negative offset to fake old uptime
                self.metrics.start_time = time.time() - 200  # 200s ago
                self.metrics.last_heartbeat = time.time() - 100  # 100s ago

        async def _heartbeat_loop(self, interval: float = 10.0) -> None:
            pass  # silence the heartbeat loop so last_heartbeat stays stale

    registry = make_registry()
    sup = make_supervisor(registry)
    sup.HEARTBEAT_TIMEOUT = 35.0  # keep default

    def factory() -> _TestActor:
        return SilentActor(name="silent-3")

    sup.supervise("silent-3", factory, max_restarts=3, restart_delay=0.0)
    await sup.start()

    restarted = await wait_until(lambda: spawn_count["n"] >= 2)
    await sup.stop()

    result("Actor restarted after heartbeat silence", restarted)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — Intentional stop NOT restarted
# ═══════════════════════════════════════════════════════════════════════════════


async def test4_intentional_stop() -> None:
    print("\nTest 4 — Intentional stop NOT restarted")

    spawn_count = {"n": 0}

    class NormalActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory() -> NormalActor:
        return NormalActor(name="normal-4")

    sup.supervise("normal-4", factory, max_restarts=5, restart_delay=0.0)
    await sup.start()

    await wait_until(lambda: spawn_count["n"] >= 1)

    # Intentional stop — call release() then stop()
    spec = sup._specs.get("normal-4")
    actor = spec.actor if spec else None
    result("Actor was supervised before the stop", actor is not None)
    assert actor is not None
    sup.release("normal-4")  # unlink from supervision
    await actor.stop()

    await settle(sup)  # several watch-loop cycles in which it must NOT act
    await sup.stop()

    result("Actor NOT restarted after intentional stop", spawn_count["n"] == 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — Intentional delete NOT restarted
# ═══════════════════════════════════════════════════════════════════════════════


async def test5_intentional_delete() -> None:
    print("\nTest 5 — Intentional delete NOT restarted")

    spawn_count = {"n": 0}

    class NormalActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory() -> NormalActor:
        return NormalActor(name="normal-5")

    sup.supervise("normal-5", factory, max_restarts=5, restart_delay=0.0)
    await sup.start()

    await wait_until(lambda: spawn_count["n"] >= 1)

    spec = sup._specs.get("normal-5")
    actor = spec.actor if spec else None
    result("Actor was supervised before the delete", actor is not None)
    assert actor is not None
    sup.release("normal-5")  # unlink
    await registry.unregister(actor.actor_id)
    await actor.stop()

    await settle(sup)
    await sup.stop()

    result("Actor NOT restarted after intentional delete", spawn_count["n"] == 1)
    result("Deleted actor stays out of the registry", registry.get(actor.actor_id) is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5b — release() alone stops supervision, without relying on actor state
# ═══════════════════════════════════════════════════════════════════════════════


async def test5b_release_alone_unlinks() -> None:
    """A released spec is not restarted even when its actor looks crashed.

    Tests 4 and 5 stop the actor as well, so the watch loop's separate
    STOPPED skip covers for `release()` — they still pass with the retired flag
    disabled entirely. This one leaves the actor FAILED, which the
    state guard does *not* skip, so only the retired flag can prevent a restart.
    """
    print("\nTest 5b — release() unlinks even a FAILED actor")

    spawn_count = {"n": 0}

    class NormalActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1

    registry = make_registry()
    sup = make_supervisor(registry)

    def factory() -> NormalActor:
        return NormalActor(name="released-5b")

    sup.supervise("released-5b", factory, max_restarts=5, restart_delay=0.0)
    await sup.start()
    await wait_until(lambda: spawn_count["n"] >= 1)

    spec = sup._specs.get("released-5b")
    assert spec
    actor = spec.actor if spec else None
    assert actor is not None

    sup.release("released-5b")
    actor.state = ActorState.FAILED  # would be restarted if the spec were live

    await settle(sup)
    await sup.stop()

    result("Released spec is not restarted even when FAILED", spawn_count["n"] == 1)
    result("release() marks the spec retired", spec.retired is True)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — Budget exhaustion retires the spec (no infinite loop)
# ═══════════════════════════════════════════════════════════════════════════════


async def test6_budget_exhaustion() -> None:
    print("\nTest 6 — Budget exhaustion retires spec (no infinite loop)")

    spawn_count = {"n": 0}

    class AlwaysCrashActor(_TestActor):
        async def on_start(self) -> None:
            spawn_count["n"] += 1
            self.state = ActorState.FAILED  # always crash immediately

    registry = make_registry()
    sup = make_supervisor(registry)

    MAX = 3

    def factory() -> AlwaysCrashActor:
        return AlwaysCrashActor(name="always-crash-6")

    sup.supervise(
        "always-crash-6",
        factory,
        max_restarts=MAX,
        restart_window=60.0,
        restart_delay=0.0,
    )
    await sup.start()

    # Wait for retirement rather than guessing how long the budget takes to burn.
    await wait_until(lambda: getattr(sup._specs.get("always-crash-6"), "retired", False))

    spec = sup._specs.get("always-crash-6")
    retired = spec.retired if spec else False

    # After budget is gone, watch loop must not keep calling restart
    count_at_retirement = spawn_count["n"]
    await settle(sup)  # more cycles — the count must not grow
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


async def main() -> None:
    print("=" * 60)
    print("  Wactorz Supervisor Test Suite")
    print("=" * 60)

    for test in (
        test1_failed_state,
        test2_error_storm,
        test3_heartbeat_silence,
        test4_intentional_stop,
        test5_intentional_delete,
        test5b_release_alone_unlinks,
        test6_budget_exhaustion,
    ):
        # result() asserts now, so catch here to keep this runner's documented
        # "run them all, report at the end" behaviour. pytest isolates them itself.
        try:
            await test()
        except AssertionError:
            result._any_failed = True

    print()
    if result._any_failed:
        print("  ❌  Some tests FAILED — see above.")
        sys.exit(1)
    else:
        print("  ✅  All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
