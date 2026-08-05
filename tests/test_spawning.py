"""Tests for ``wactorz.agents.mixins.spawning.SpawnMixin``.

These exercise the real mixin against a lightweight fake Actor host. Routing is
checked by the spawned class name (so the test does not depend on where each
agent module lives), and the only external seam stubbed is the topic bus, via
``monkeypatch``. No broker, persistence backend, or installer agent is required.

Run with ``pytest`` (or ``make test-py``). Async mixin methods are driven through
``asyncio.run`` so no ``pytest-asyncio`` plugin is needed.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from wactorz.agents.main_actor import MainActor
from wactorz.agents.mixins.spawning import SpawnMixin, SpawnPlaceholder


def run(coro):
    """Drive a coroutine to completion without pytest-asyncio."""
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeActor:
    def __init__(self, name):
        self.name = name
        self.actor_id = f"id-{name}"
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeRegistry:
    def __init__(self):
        self._by_name = {}
        self._supervisor_ref = None

    def add(self, actor):
        self._by_name[actor.name] = actor

    def find_by_name(self, name):
        return self._by_name.get(name)

    async def unregister(self, actor_id):
        for n, a in list(self._by_name.items()):
            if a.actor_id == actor_id:
                del self._by_name[n]


class _BaseHost(SpawnMixin):
    """Supplies the Actor-base surface the mixin relies on."""

    def __init__(self, registry, name):
        self.name = name
        self.actor_id = f"id-{name}"
        self.llm = object()
        self._registry = registry
        self._result_futures = {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / name  # .parent is the base
        self.spawn_calls = []  # (cls, kwargs)
        self.sent = []  # installer payloads
        self.published = []  # mqtt dashboard echoes

    async def spawn(self, actor_class, **kwargs):
        self.spawn_calls.append((actor_class, kwargs))
        actor = FakeActor(kwargs.get("name", "anon"))
        self._registry.add(actor)
        return actor

    async def send(self, target_id, msg_type, payload):
        self.sent.append(payload)
        # Resolve the install future immediately, standing in for the installer.
        fut = self._result_futures.get(payload.get("_task_id"))
        if fut is not None and not fut.done():
            fut.set_result({"message": "installed ok"})

    async def _mqtt_publish(self, topic, payload, **_kw):
        self.published.append((topic, payload))


class MainHost(_BaseHost):
    """Owns the spawn registry and user facts, like MainActor."""

    def __init__(self, registry):
        super().__init__(registry, "main")
        self.registered = []
        self._agent_manifests = {}

    def _save_to_spawn_registry(self, config):
        self.registered.append(config)

    def get_user_facts(self):
        return {"pref_timezone": "Europe/Athens"}


class PeerHost(_BaseHost):
    """No registry/facts ownership, like PlannerAgent."""

    def __init__(self, registry):
        super().__init__(registry, "planner-x")


class FakeMain(MainActor):
    """A real MainActor by type, with the two methods the peer path calls recorded.

    It has to be a real one: `find_main_actor` resolves main with `isinstance`,
    so a stand-in that merely has the right attributes resolves to None and the
    peer registration silently does nothing — which is the behaviour the check
    exists to produce.
    """

    def __init__(self, persistence_dir: str) -> None:
        super().__init__(llm_provider=None, name="main", persistence_dir=persistence_dir)
        self.registered: list[dict] = []
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    def _save_to_spawn_registry(self, config: dict) -> None:
        self.registered.append(config)

    def get_user_facts(self) -> dict:
        return {"pref_timezone": "Europe/Athens"}


@pytest.fixture
def main_host():
    return MainHost(FakeRegistry())


@pytest.fixture
def peer_setup(tmp_path: Path):
    reg = FakeRegistry()
    # tmp_path: Actor.__init__ creates its persistence directory, so a default
    # one would write ./actor_state/main into the working copy.
    main = FakeMain(str(tmp_path))
    reg.add(main)
    return PeerHost(reg), reg, main


# ── Routing (checked by spawned class name) ──────────────────────────────────


def test_route_dynamic(main_host):
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "cpu", "type": "dynamic", "code": "async def setup(a): pass"}
        )
    )
    assert actor is not None
    cls, kw = main_host.spawn_calls[-1]
    assert cls.__name__ == "DynamicAgent"
    assert kw["name"] == "cpu"


def test_route_llm_explicit(main_host):
    run(main_host._spawn_local_from_config({"name": "q", "type": "llm", "system_prompt": "hi"}))
    assert main_host.spawn_calls[-1][0].__name__ == "LLMAgent"


def test_route_llm_implicit(main_host):
    # No type (defaults to 'dynamic'), no code, but has a system prompt.
    run(main_host._spawn_local_from_config({"name": "q2", "system_prompt": "you are helpful"}))
    assert main_host.spawn_calls[-1][0].__name__ == "LLMAgent"


def test_route_scheduled_injects_timezone(main_host):
    run(
        main_host._spawn_local_from_config(
            {"name": "morning", "type": "scheduled", "schedule": {"type": "daily", "at": "07:00"}}
        )
    )
    cls, kw = main_host.spawn_calls[-1]
    assert cls.__name__ == "ScheduledAgent"
    assert kw["timezone"] == "Europe/Athens"


def test_route_scheduled_invalid_returns_none(main_host):
    actor = run(main_host._spawn_local_from_config({"name": "bad", "type": "scheduled"}))
    assert actor is None
    assert not main_host.spawn_calls


def test_route_ha_actuator(main_host):
    run(
        main_host._spawn_local_from_config(
            {"name": "lights-on", "type": "ha_actuator", "automation_id": "lights-on"}
        )
    )
    cls, kw = main_host.spawn_calls[-1]
    assert cls.__name__ == "HomeAssistantActuatorAgent"
    assert kw["name"] == "lights-on"


def test_ha_actuator_internal_rename(main_host):
    # The handler's defensive rename, exercised directly (the dispatcher is the
    # primary collision guard, so we bypass it here).
    main_host._registry.add(FakeActor("lights-on"))
    run(
        main_host._spawn_ha_actuator(
            {"name": "lights-on", "automation_id": "lights-on"}, "lights-on"
        )
    )
    name = main_host.spawn_calls[-1][1]["name"]
    assert name != "lights-on" and name.startswith("lights-on-")


def test_route_native(main_host):
    # Native agents carry no code/prompt — the factory is re-resolved by name
    # from the catalog. weather-agent has no optional deps, so it always resolves.
    actor = run(main_host._spawn_local_from_config({"name": "weather-agent", "type": "native"}))
    assert actor is not None
    cls, kw = main_host.spawn_calls[-1]
    assert cls.__name__ == "WeatherAgent"
    assert kw["name"] == "weather-agent"


def test_native_persisted_for_restore(main_host):
    # The regression under fix: a native agent must land in the spawn registry
    # so it is restored after a process restart (previously it never was).
    run(main_host._spawn_local_from_config({"name": "weather-agent", "type": "native"}))
    assert [c["name"] for c in main_host.registered] == ["weather-agent"]


def test_native_unknown_returns_none(main_host):
    actor = run(main_host._spawn_local_from_config({"name": "no-such-native", "type": "native"}))
    assert actor is None
    assert not main_host.spawn_calls


def test_unknown_type_returns_none(main_host):
    assert run(main_host._spawn_local_from_config({"name": "x", "type": "wat"})) is None


def test_no_code_no_prompt_returns_none(main_host):
    assert run(main_host._spawn_local_from_config({"name": "empty", "type": "dynamic"})) is None


# ── Idempotency / replace ────────────────────────────────────────────────────


def test_existing_no_replace_returns_existing(main_host):
    pre = FakeActor("dup")
    main_host._registry.add(pre)
    actor = run(main_host._spawn_local_from_config({"name": "dup", "type": "dynamic", "code": "x"}))
    assert actor is pre
    assert not main_host.spawn_calls


def test_existing_with_replace(main_host):
    pre = FakeActor("dup")
    main_host._registry.add(pre)
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "dup", "type": "dynamic", "code": "x", "replace": True}
        )
    )
    assert pre.stopped
    assert main_host.spawn_calls and actor is not pre


# ── Install models ───────────────────────────────────────────────────────────


def test_present_packages_spawn_directly(main_host):
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "d", "type": "dynamic", "code": "x", "install": ["os", "json"]}
        )
    )
    assert not isinstance(actor, SpawnPlaceholder)
    assert not main_host.sent  # installer never contacted


def test_blocking_install(main_host):
    main_host._registry.add(FakeActor("installer"))
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "d2", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=True,
        )
    )
    assert not isinstance(actor, SpawnPlaceholder)
    assert main_host.sent and main_host.sent[0]["action"] == "install"
    assert main_host.spawn_calls


def test_background_install_returns_placeholder(main_host):
    main_host._registry.add(FakeActor("installer"))

    async def scenario():
        actor = await main_host._spawn_local_from_config(
            {"name": "d3", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=False,
        )
        assert isinstance(actor, SpawnPlaceholder)
        await asyncio.sleep(0.1)  # let the background task finish

    run(scenario())
    assert main_host.spawn_calls
    assert main_host.registered
    echoed = [p.get("type") for _, p in main_host.published]
    assert "log" in echoed and "spawned" in echoed


def test_install_fast_path_no_send(main_host):
    main_host._registry.add(FakeActor("installer"))
    run(main_host._install_packages(["os", "json"], agent_name="x"))
    assert not main_host.sent


# ── Flags / wiring ───────────────────────────────────────────────────────────


def test_trusted_flag_passthrough(main_host):
    run(
        main_host._spawn_local_from_config(
            {"name": "cat", "type": "dynamic", "code": "x", "trusted": True}
        )
    )
    assert main_host.spawn_calls[-1][1]["trusted"] is True


def test_topiccontract_registered(monkeypatch, main_host):
    recorded = []

    class _RecorderBus:
        def register_contract(self, contract):
            recorded.append(contract)

    monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: _RecorderBus())
    run(
        main_host._spawn_local_from_config(
            {
                "name": "pub",
                "type": "dynamic",
                "code": "x",
                "publishes": ["sensors/cpu"],
                "subscribes": [],
            }
        )
    )
    assert len(recorded) == 1


def test_topiccontract_skipped_without_pubsub(monkeypatch, main_host):
    recorded = []
    monkeypatch.setattr(
        "wactorz.core.topic_bus.get_topic_bus",
        lambda: type("B", (), {"register_contract": lambda self, c: recorded.append(c)})(),
    )
    run(main_host._spawn_local_from_config({"name": "nopub", "type": "dynamic", "code": "x"}))
    assert recorded == []


# ── Registry & timezone hooks (main vs peer) ─────────────────────────────────


def test_register_main_direct(main_host):
    run(main_host._spawn_local_from_config({"name": "r", "type": "dynamic", "code": "x"}))
    assert [c["name"] for c in main_host.registered] == ["r"]


def test_register_peer_routes_to_main(peer_setup):
    host, reg, main = peer_setup
    reg.add(FakeActor("installer"))
    run(host._spawn_local_from_config({"name": "p", "type": "dynamic", "code": "x"}))
    assert [c["name"] for c in main.registered] == ["p"]


def test_register_skipped_when_disabled(main_host):
    run(
        main_host._spawn_local_from_config(
            {"name": "noreg", "type": "dynamic", "code": "x"}, register=False
        )
    )
    assert not main_host.registered


def test_peer_resolves_timezone_from_main(peer_setup):
    host, _reg, _main = peer_setup
    run(
        host._spawn_local_from_config(
            {"name": "sched", "type": "scheduled", "schedule": {"type": "daily", "at": "07:00"}}
        )
    )
    assert host.spawn_calls[-1][1]["timezone"] == "Europe/Athens"


# ── Native catalog resolver & registry safety ────────────────────────────────


def test_get_native_factory_resolves_and_misses():
    from wactorz.agents.catalog_agent import get_native_factory

    assert get_native_factory("weather-agent").__name__ == "WeatherAgent"
    assert get_native_factory("not-a-catalog-name") is None


def test_native_recipes_are_json_safe_without_factory():
    # CatalogAgent persists each native recipe minus its 'factory' class object;
    # that descriptor must be JSON-serializable for every native recipe so the
    # spawn registry (SQLite/JSON) can store and later restore it.
    import json

    from wactorz.agents.catalog_agent import _build_native_catalog

    native = _build_native_catalog()
    assert native, "expected at least one native catalog recipe (weather-agent)"
    for recipe in native.values():
        save_config = {k: v for k, v in recipe.items() if k != "factory"}
        json.dumps(save_config)  # must not raise for any native recipe
