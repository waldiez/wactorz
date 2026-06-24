"""
Runnable test suite for wactorz.agents.spawning.SpawnMixin.

Self-contained: uses fake host/registry/actor objects and the fake sibling
modules in this test package, so it exercises the REAL mixin source without the
heavy production dependencies. Run with:  python tests/test_spawning.py
Exits non-zero if any assertion fails.
"""

import asyncio
import sys
import tempfile
import traceback
from pathlib import Path

# Make `wactorz` importable from this test package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wactorz.agents.mixins.spawning import SpawnMixin, _SpawnPlaceholder
from wactorz.agents import dynamic_agent, llm_agent, scheduled_agent, manual_agent
from wactorz.agents import home_assistant_actuator_agent as ha_mod
from wactorz.core import topic_bus as bus_mod
from wactorz.core.actor import MessageType


# ── Fakes ──────────────────────────────────────────────────────────────────

class FakeActor:
    def __init__(self, name):
        self.name = name
        self.actor_id = f"id-{name}"
        self.stopped = False

    async def stop(self):
        self.stopped = True

    def recall(self, *_a, **_k):
        return None


class FakeRegistry:
    def __init__(self):
        self._by_name = {}
        self._supervisor_ref = None

    def add(self, actor):
        self._by_name[actor.name] = actor

    def find_by_name(self, name):
        return self._by_name.get(name)

    def all_actors(self):
        return list(self._by_name.values())

    async def unregister(self, actor_id):
        for n, a in list(self._by_name.items()):
            if a.actor_id == actor_id:
                del self._by_name[n]


class _BaseHost(SpawnMixin):
    """Shared fake Actor surface the mixin depends on."""

    def __init__(self, registry, name):
        self.name = name
        self.actor_id = f"id-{name}"
        self.llm = object()
        self._registry = registry
        self._result_futures = {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / name  # .parent is the base
        self.spawn_calls = []  # list of (cls, kwargs)
        self.sent = []         # list of payloads sent to installer
        self.published = []    # list of (topic, payload) mqtt echoes

    async def _mqtt_publish(self, topic, payload, **_kw):
        self.published.append((topic, payload))

    async def spawn(self, actor_class, **kwargs):
        self.spawn_calls.append((actor_class, kwargs))
        actor = FakeActor(kwargs.get("name", "anon"))
        self._registry.add(actor)
        return actor

    async def send(self, target_id, msg_type, payload):
        self.sent.append(payload)
        # Simulate the installer replying immediately so _install_packages'
        # awaited future resolves without a real installer.
        tid = payload.get("_task_id")
        fut = self._result_futures.get(tid)
        if fut is not None and not fut.done():
            fut.set_result({"message": "installed ok"})


class MainHost(_BaseHost):
    """Behaves like MainActor: owns the spawn registry and the user facts."""

    def __init__(self, registry, name="main"):
        super().__init__(registry, name)
        self.registered = []          # configs written to the registry
        self._agent_manifests = {}    # only MainActor has this cache

    def _save_to_spawn_registry(self, config):
        self.registered.append(config)

    def get_user_facts(self):
        return {"pref_timezone": "Europe/Athens"}


class PeerHost(_BaseHost):
    """Behaves like PlannerAgent: no registry ownership, no facts of its own."""

    def __init__(self, registry, name="planner-abc"):
        super().__init__(registry, name)


class FakeMain(FakeActor):
    """A 'main' actor sitting in the registry for peer-routing tests."""

    def __init__(self):
        super().__init__("main")
        self.registered = []

    def _save_to_spawn_registry(self, config):
        self.registered.append(config)

    def get_user_facts(self):
        return {"pref_timezone": "Europe/Athens"}


# ── Test harness ────────────────────────────────────────────────────────────

RESULTS = []

def check(cond, msg):
    if not cond:
        raise AssertionError(msg)

async def run(label, coro):
    try:
        await coro
        RESULTS.append((label, True, ""))
        print(f"  PASS  {label}")
    except Exception as e:
        RESULTS.append((label, False, f"{e}\n{traceback.format_exc()}"))
        print(f"  FAIL  {label}: {e}")


def fresh_main():
    reg = FakeRegistry()
    return MainHost(reg), reg

def fresh_peer():
    reg = FakeRegistry()
    main = FakeMain()
    reg.add(main)
    return PeerHost(reg), reg, main


# ── Tests ───────────────────────────────────────────────────────────────────

async def t_route_dynamic():
    host, _ = fresh_main()
    actor = await host._spawn_local_from_config(
        {"name": "cpu", "type": "dynamic", "code": "async def setup(a): pass"}
    )
    check(actor is not None, "dynamic actor should be returned")
    cls, kw = host.spawn_calls[-1]
    check(cls is dynamic_agent.DynamicAgent, "should spawn DynamicAgent")
    check(kw["name"] == "cpu", "name forwarded")

async def t_route_llm_explicit():
    host, _ = fresh_main()
    await host._spawn_local_from_config({"name": "q", "type": "llm", "system_prompt": "hi"})
    cls, _ = host.spawn_calls[-1]
    check(cls is llm_agent.LLMAgent, "explicit type=llm should spawn LLMAgent")

async def t_route_llm_implicit():
    # No type (defaults to 'dynamic'), no code, but has a system_prompt.
    host, _ = fresh_main()
    await host._spawn_local_from_config({"name": "q2", "system_prompt": "you are helpful"})
    cls, _ = host.spawn_calls[-1]
    check(cls is llm_agent.LLMAgent, "implicit llm route should spawn LLMAgent")

async def t_route_scheduled_and_timezone():
    host, _ = fresh_main()
    await host._spawn_local_from_config(
        {"name": "morning", "type": "scheduled", "schedule": {"type": "daily", "at": "07:00"}}
    )
    cls, kw = host.spawn_calls[-1]
    check(cls is scheduled_agent.ScheduledAgent, "should spawn ScheduledAgent")
    check(kw["timezone"] == "Europe/Athens", "user timezone injected from facts")

async def t_route_scheduled_invalid():
    host, _ = fresh_main()
    actor = await host._spawn_local_from_config({"name": "bad", "type": "scheduled"})
    check(actor is None, "missing schedule dict should return None")
    check(not host.spawn_calls, "nothing should be spawned")

async def t_route_manual():
    host, _ = fresh_main()
    await host._spawn_local_from_config({"name": "man", "type": "manual"})
    cls, _ = host.spawn_calls[-1]
    check(cls is manual_agent.ManualAgent, "should spawn ManualAgent")

async def t_route_ha_actuator():
    # Free name → dispatcher routes straight to the actuator handler.
    host, _ = fresh_main()
    await host._spawn_local_from_config(
        {"name": "lights-on", "type": "ha_actuator", "automation_id": "lights-on"}
    )
    cls, kw = host.spawn_calls[-1]
    check(cls is ha_mod.HomeAssistantActuatorAgent, "should spawn actuator")
    check(kw["name"] == "lights-on", "name forwarded when no collision")

async def t_ha_actuator_internal_rename():
    # The handler's own defensive rename, exercised in isolation (the dispatcher
    # is the primary collision guard, so we call the handler directly here).
    host, reg = fresh_main()
    reg.add(FakeActor("lights-on"))  # pre-existing name
    await host._spawn_ha_actuator(
        {"name": "lights-on", "automation_id": "lights-on"}, "lights-on"
    )
    _, kw = host.spawn_calls[-1]
    check(kw["name"] != "lights-on" and kw["name"].startswith("lights-on-"),
          f"actuator should suffix a colliding name, got {kw['name']}")

async def t_unknown_type():
    host, _ = fresh_main()
    actor = await host._spawn_local_from_config({"name": "x", "type": "wat"})
    # 'wat' has no code/prompt → falls through to the neither-branch → None
    check(actor is None, "unknown type with no code/prompt → None")

async def t_no_code_no_prompt():
    host, _ = fresh_main()
    actor = await host._spawn_local_from_config({"name": "empty", "type": "dynamic"})
    check(actor is None, "dynamic with no code → None")

async def t_existing_no_replace():
    host, reg = fresh_main()
    pre = FakeActor("dup")
    reg.add(pre)
    actor = await host._spawn_local_from_config({"name": "dup", "type": "dynamic", "code": "x"})
    check(actor is pre, "should return the existing actor")
    check(not host.spawn_calls, "should NOT spawn a duplicate")

async def t_existing_with_replace():
    host, reg = fresh_main()
    pre = FakeActor("dup")
    reg.add(pre)
    actor = await host._spawn_local_from_config(
        {"name": "dup", "type": "dynamic", "code": "x", "replace": True}
    )
    check(pre.stopped, "old actor should be stopped on replace")
    check(host.spawn_calls, "a new actor should be spawned")
    check(actor is not pre, "returned actor should be the new one")

async def t_dynamic_all_pkgs_present():
    host, _ = fresh_main()
    # 'os' and 'json' are importable → no install needed, spawn directly.
    actor = await host._spawn_local_from_config(
        {"name": "d", "type": "dynamic", "code": "x", "install": ["os", "json"]}
    )
    check(not isinstance(actor, _SpawnPlaceholder), "present pkgs → real actor, not placeholder")
    check(not host.sent, "installer should NOT be contacted when nothing is needed")

async def t_dynamic_blocking_install():
    host, reg = fresh_main()
    reg.add(FakeActor("installer"))
    actor = await host._spawn_local_from_config(
        {"name": "d2", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
        blocking_install=True,
    )
    check(not isinstance(actor, _SpawnPlaceholder), "blocking install → real actor")
    check(host.sent and host.sent[0]["action"] == "install", "installer should be contacted")
    check(host.spawn_calls, "agent should be spawned after install")

async def t_dynamic_background_install():
    host, reg = fresh_main()
    reg.add(FakeActor("installer"))
    actor = await host._spawn_local_from_config(
        {"name": "d3", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
        blocking_install=False,
    )
    check(isinstance(actor, _SpawnPlaceholder), "background install → placeholder returned first")
    await asyncio.sleep(0.1)  # let the background task finish
    check(host.spawn_calls, "background task should eventually spawn the agent")
    check(host.registered, "background path should register the config with main")
    types = [p.get("type") for _, p in host.published]
    check("log" in types and "spawned" in types,
          "background path should emit 'log' and 'spawned' dashboard echoes")

async def t_trusted_flag_passthrough():
    host, _ = fresh_main()
    await host._spawn_local_from_config(
        {"name": "cat", "type": "dynamic", "code": "x", "trusted": True}
    )
    _, kw = host.spawn_calls[-1]
    check(kw["trusted"] is True, "trusted flag must reach DynamicAgent")

async def t_topiccontract_registered():
    bus_mod.REGISTERED.clear()
    host, _ = fresh_main()
    await host._spawn_local_from_config(
        {"name": "pub", "type": "dynamic", "code": "x",
         "publishes": ["sensors/cpu"], "subscribes": []}
    )
    check(len(bus_mod.REGISTERED) == 1, "a TopicContract should be registered")
    check(bus_mod.REGISTERED[0].publishes == ["sensors/cpu"], "contract carries publishes")

async def t_topiccontract_skipped_without_pubsub():
    bus_mod.REGISTERED.clear()
    host, _ = fresh_main()
    await host._spawn_local_from_config({"name": "nopub", "type": "dynamic", "code": "x"})
    check(len(bus_mod.REGISTERED) == 0, "no contract when no publishes/subscribes")

async def t_register_main_direct():
    host, _ = fresh_main()
    await host._spawn_local_from_config({"name": "r", "type": "dynamic", "code": "x"})
    check(len(host.registered) == 1, "main should write its own spawn registry")
    check(host.registered[0]["name"] == "r", "correct config registered")

async def t_register_peer_routes_to_main():
    host, reg, main = fresh_peer()
    reg.add(FakeActor("installer"))
    await host._spawn_local_from_config({"name": "p", "type": "dynamic", "code": "x"})
    check(len(main.registered) == 1, "peer should route registration to main")
    check(main.registered[0]["name"] == "p", "correct config routed")

async def t_register_skipped_when_disabled():
    host, _ = fresh_main()
    await host._spawn_local_from_config(
        {"name": "noreg", "type": "dynamic", "code": "x"}, register=False
    )
    check(not host.registered, "register=False should skip the registry write")

async def t_peer_resolves_timezone_from_main():
    host, reg, _ = fresh_peer()
    await host._spawn_local_from_config(
        {"name": "sched", "type": "scheduled", "schedule": {"type": "daily", "at": "07:00"}}
    )
    _, kw = host.spawn_calls[-1]
    check(kw["timezone"] == "Europe/Athens", "peer should read timezone from main")

async def t_install_fast_path_no_send():
    host, reg = fresh_main()
    reg.add(FakeActor("installer"))
    await host._install_packages(["os", "json"], agent_name="x")
    check(not host.sent, "importable packages should not contact the installer")


async def main():
    tests = [
        ("route: dynamic", t_route_dynamic()),
        ("route: llm (explicit)", t_route_llm_explicit()),
        ("route: llm (implicit)", t_route_llm_implicit()),
        ("route: scheduled + tz", t_route_scheduled_and_timezone()),
        ("route: scheduled invalid", t_route_scheduled_invalid()),
        ("route: manual", t_route_manual()),
        ("route: ha_actuator", t_route_ha_actuator()),
        ("unit: ha_actuator rename", t_ha_actuator_internal_rename()),
        ("guard: unknown type", t_unknown_type()),
        ("guard: no code/prompt", t_no_code_no_prompt()),
        ("idempotency: existing no replace", t_existing_no_replace()),
        ("idempotency: existing with replace", t_existing_with_replace()),
        ("install: all present → direct", t_dynamic_all_pkgs_present()),
        ("install: blocking", t_dynamic_blocking_install()),
        ("install: background placeholder", t_dynamic_background_install()),
        ("flag: trusted passthrough", t_trusted_flag_passthrough()),
        ("bus: contract registered", t_topiccontract_registered()),
        ("bus: contract skipped", t_topiccontract_skipped_without_pubsub()),
        ("registry: main direct", t_register_main_direct()),
        ("registry: peer routes to main", t_register_peer_routes_to_main()),
        ("registry: skipped when disabled", t_register_skipped_when_disabled()),
        ("tz: peer reads main", t_peer_resolves_timezone_from_main()),
        ("install: fast path no send", t_install_fast_path_no_send()),
    ]
    print("Running SpawnMixin tests\n")
    for label, coro in tests:
        await run(label, coro)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{passed}/{len(RESULTS)} passed.")
    if failed:
        print("\nFailures:")
        for label, _, detail in failed:
            print(f"\n### {label}\n{detail}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())