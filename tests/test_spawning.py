"""Tests for ``wactorz.agents.mixins.spawning.SpawnMixin``.

These exercise the real mixin against a lightweight fake Actor host. Routing is
checked by the spawned class name (so the test does not depend on where each
agent module lives), and the only external seam stubbed is the topic bus, via
``monkeypatch``. No broker, persistence backend, or installer agent is required.

Run with ``pytest`` (or ``make test-py``). Async mixin methods are driven through
``asyncio.run`` so no ``pytest-asyncio`` plugin is needed.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from wactorz.agents.catalog_agent import _build_native_catalog, get_native_factory
from wactorz.agents.llm_agent import LLMProvider
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.spawns import SpawnService
from wactorz.agents.mixins import spawning
from wactorz.agents.mixins.spawning import SpawnMixin, SpawnPlaceholder
from wactorz.core.actor import ActorState
from wactorz.core.persistence import PickleStore, WactorzDB


def run(coro):
    """Drive a coroutine to completion without pytest-asyncio."""
    return asyncio.run(coro)


def _available_off_loop(requirements: list[str]) -> list[str]:
    with pytest.raises(RuntimeError, match="no running event loop"):
        asyncio.get_running_loop()
    assert requirements == ["numpy"]
    return []


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeActor:
    def __init__(self, name):
        self.name = name
        self.actor_id = f"id-{name}"
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeSupervisor:
    """Records the entries it is told to forget, and when, against the actors' stops."""

    def __init__(self, log: list[str]) -> None:
        self._specs: dict = {}
        self._log = log

    def drop_supervised(self, name: str) -> None:
        self._log.append(f"forget {name}")


class FakeRegistry:
    def __init__(self):
        self._by_name = {}
        self._supervisor_ref: FakeSupervisor | None = None

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
        # What the mixin hands the agents it creates; never called here.
        self.llm = cast(LLMProvider, object())
        self._registry = registry
        self.registry: FakeRegistry = registry
        self._result_futures = {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / name  # .parent is the base
        self.spawn_calls = []  # (cls, kwargs)
        self.sent = []  # installer payloads
        self.published = []  # mqtt dashboard echoes
        self.state = ActorState.RUNNING
        self.detached: list[asyncio.Task] = []  # background work the host was handed
        self.install_result: dict = {"message": "installed ok"}  # what the installer answers
        self.told: list[str] = []  # chat notices

    async def spawn(self, actor_class, **kwargs):
        self.spawn_calls.append((actor_class, kwargs))
        actor = FakeActor(kwargs.get("name", "anon"))
        self.registry.add(actor)
        return actor

    async def send(self, target_id, msg_type, payload):
        self.sent.append(payload)
        # Resolve the install future immediately, standing in for the installer.
        fut = self._result_futures.get(payload.get("_task_id"))
        if fut is not None and not fut.done():
            fut.set_result(self.install_result)

    async def notify_user(self, text, **_extra):
        self.told.append(text)

    async def _mqtt_publish(self, topic, payload, **_kw):
        self.published.append((topic, payload))

    def persist(self, key, value):
        pass

    def recall(self, key, default=None):
        return default

    def run_detached(self, coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        self.detached.append(task)
        return task


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


def test_a_replaced_agent_leaves_supervision_before_it_stops(main_host):
    # The replacement takes a fresh entry when it is spawned. If that spawn
    # fails, an entry left holding the stopped agent would be stopped again at
    # shutdown; and forgetting it first keeps the watch loop off the agent while
    # it stops.
    log: list[str] = []
    main_host._registry._supervisor_ref = FakeSupervisor(log)
    pre = FakeActor("dup")

    async def _stop() -> None:
        log.append("stop dup")

    pre.stop = _stop  # pyright: ignore[reportAttributeAccessIssue]  # records the order
    main_host._registry.add(pre)

    run(
        main_host._spawn_local_from_config(
            {"name": "dup", "type": "dynamic", "code": "x", "replace": True}
        )
    )

    assert log == ["forget dup", "stop dup"]


# ── Install models ───────────────────────────────────────────────────────────


def test_present_packages_spawn_directly(main_host):
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "d", "type": "dynamic", "code": "x", "install": ["os", "json"]}
        )
    )
    assert not isinstance(actor, SpawnPlaceholder)
    assert not main_host.sent  # installer never contacted


@pytest.mark.parametrize("action", ["spawn", "install"])
def test_requirement_scans_run_off_the_event_loop(
    main_host: MainHost, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setattr(spawning, "missing_requirements", _available_off_loop)
    if action == "spawn":
        actor = run(
            main_host._spawn_local_from_config(
                {"name": "d", "type": "dynamic", "code": "x", "install": ["numpy"]}
            )
        )
        assert actor is not None
        assert not isinstance(actor, SpawnPlaceholder)
    else:
        assert run(main_host._install_packages(["numpy"])).ok
    assert not main_host.sent


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


def test_a_failed_install_is_reported_and_not_spawned(main_host):
    main_host._registry.add(FakeActor("installer"))
    main_host.install_result = {
        "failed": ["totally_missing_pkg_zzz"],
        "results": {"totally_missing_pkg_zzz": "failed: ERROR: no matching distribution"},
    }
    actor = run(
        main_host._spawn_local_from_config(
            {"name": "d6", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=True,
        )
    )
    assert actor is None
    assert main_host.spawn_calls == []
    assert main_host.registered == []
    (told,) = main_host.told
    assert "totally_missing_pkg_zzz: ERROR: no matching distribution" in told


def test_an_install_needing_a_restart_keeps_the_agent_for_after_it(main_host):
    main_host._registry.add(FakeActor("installer"))
    main_host.install_result = {"failed": [], "restart_required": ["websockets 17.1 -> 15.0.1"]}

    async def scenario():
        await main_host._spawn_local_from_config(
            {"name": "d7", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=False,
        )
        await asyncio.gather(*main_host.detached)

    run(scenario())
    assert main_host.spawn_calls == []
    # Kept (the real registry is keyed by name), so the restart brings it up.
    assert {cfg["name"] for cfg in main_host.registered} == {"d7"}
    (told,) = main_host.told
    assert "restart Wactorz" in told


def test_the_install_request_asks_for_chat_progress(main_host):
    main_host._registry.add(FakeActor("installer"))
    run(main_host._install_packages(["totally_missing_pkg_zzz"], agent_name="d8"))
    (request,) = main_host.sent
    assert request["notify"] is True
    assert request["for_agent"] == "d8"


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
    factory = get_native_factory("weather-agent")
    assert factory is not None
    assert factory.__name__ == "WeatherAgent"
    assert get_native_factory("not-a-catalog-name") is None


def test_native_recipes_are_json_safe_without_factory():
    # CatalogAgent persists each native recipe minus its 'factory' class object;
    # that descriptor must be JSON-serializable for every native recipe so the
    # spawn registry (SQLite/JSON) can store and later restore it.
    native = _build_native_catalog()
    assert native, "expected at least one native catalog recipe (weather-agent)"
    for recipe in native.values():
        save_config = {k: v for k, v in recipe.items() if k != "factory"}
        json.dumps(save_config)  # must not raise for any native recipe


# ── The `trusted` flag is earned, not declared ───────────────────────────────


def _dynamic(**extra: object) -> dict:
    """A minimal dynamic-agent spawn config, plus whatever the test is about."""
    return {"name": "probe", "type": "dynamic", "code": "async def setup(a): pass", **extra}


def _trusted_kwarg(host: MainHost) -> object:
    cls, kwargs = host.spawn_calls[-1]
    assert cls.__name__ == "DynamicAgent"
    return kwargs["trusted"]


def test_a_model_authored_config_cannot_grant_itself_trust(main_host: MainHost) -> None:
    # `trusted` skips the sanitizer and the code validator outright, and a spawn
    # config is routinely written by an LLM. Nothing but a restore may set it.
    run(main_host._spawn_local_from_config(_dynamic(trusted=True)))

    assert _trusted_kwarg(main_host) is False


def test_a_restored_config_keeps_it(main_host: MainHost) -> None:
    # The other half: catalog agents are stored trusted, and their code is not
    # written to pass the validator, so losing the flag would break the restore.
    run(main_host._spawn_local_from_config(_dynamic(trusted=True), from_registry=True))

    assert _trusted_kwarg(main_host) is True


def test_the_flag_cannot_be_laundered_through_the_registry(main_host: MainHost) -> None:
    # Registration happens after the strip, so a rejected flag cannot be
    # persisted and come back earned on the next restart.
    run(main_host._spawn_local_from_config(_dynamic(trusted=True)))

    assert main_host.registered
    assert "trusted" not in main_host.registered[-1]


def test_the_callers_own_config_is_left_alone(main_host: MainHost) -> None:
    # Callers reuse the dict they passed — for a retry, or in an error message.
    config = _dynamic(trusted=True)

    run(main_host._spawn_local_from_config(config))

    assert config["trusted"] is True


def test_a_config_without_the_flag_is_passed_through_unchanged(main_host: MainHost) -> None:
    config = _dynamic()

    run(main_host._spawn_local_from_config(config))

    assert _trusted_kwarg(main_host) is False


def test_the_refusal_is_logged_with_the_agent_name(main_host: MainHost, caplog) -> None:
    # Silent stripping would look like the validator rejecting good code.
    with caplog.at_level("WARNING"):
        run(main_host._spawn_local_from_config(_dynamic(trusted=True)))

    assert "probe" in caplog.text
    assert "trusted" in caplog.text


def test_a_migrating_catalog_agent_gets_its_trust_back(tmp_path: Path) -> None:
    # Sent out to a node, then migrated home. Without this the recipe's code
    # meets the validator it was never written to pass, and a catalog agent
    # that used to migrate fine stops migrating.
    main = MainActor(llm_provider=None, name="main", persistence_dir=str(tmp_path))
    main._save_to_spawn_registry({"name": "doc-to-pptx", "trusted": True})
    local_cfg: dict = {"name": "doc-to-pptx"}

    earned = main._restore_earned_trust("doc-to-pptx", local_cfg)

    assert earned is True
    assert local_cfg["trusted"] is True


def test_a_node_cannot_claim_trust_the_registry_never_granted(tmp_path: Path) -> None:
    # The config arrives over MQTT, where a publisher can say anything. Our own
    # record is the evidence; the wire is not.
    main = MainActor(llm_provider=None, name="main", persistence_dir=str(tmp_path))
    main._save_to_spawn_registry({"name": "scraper"})
    local_cfg: dict = {"name": "scraper", "trusted": True}

    earned = main._restore_earned_trust("scraper", local_cfg)

    # Left in place on purpose: the spawn path strips it and logs that it did.
    assert earned is False
    assert local_cfg["trusted"] is True


def test_an_agent_main_never_registered_earns_nothing(tmp_path: Path) -> None:
    main = MainActor(llm_provider=None, name="main", persistence_dir=str(tmp_path))

    assert main._restore_earned_trust("never-seen", {"name": "never-seen"}) is False


# ── A name every topic of the agent would carry ──────────────────────────────


def test_a_name_that_cannot_be_a_topic_level_is_refused_locally(main_host):
    # "c++ monitor" is an ordinary thing to ask for; its topics would all carry
    # a wildcard, and a message to it once stalled every message behind it.
    assert run(main_host._spawn_local_from_config({"name": "c++ monitor"})) is None
    assert main_host.spawn_calls == []


def test_a_name_that_cannot_be_a_topic_level_is_not_sent_to_a_node():
    published: list[str] = []

    async def _publish(topic, payload, **_kw):
        published.append(topic)

    host = SimpleNamespace(name="main", _mqtt_publish=_publish)
    run(SpawnService(host)._spawn_remote({"name": "all#"}, "rpi", save=True))  # pyright: ignore[reportArgumentType]

    assert published == []


def test_a_background_install_is_owned_by_the_host(main_host):
    # Kept by the host, so its stop cancels the install rather than leaving it
    # running against a system that has shut down.
    main_host._registry.add(FakeActor("installer"))

    async def scenario():
        await main_host._spawn_local_from_config(
            {"name": "d4", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=False,
        )
        assert [task.get_name() for task in main_host.detached] == ["install-d4"]
        await asyncio.gather(*main_host.detached)

    run(scenario())


def test_an_install_that_outlasts_a_stop_spawns_nothing(main_host):
    main_host._registry.add(FakeActor("installer"))
    real_install = main_host._install_packages

    async def install_then_stop(packages, agent_name):
        await real_install(packages, agent_name=agent_name)
        main_host.state = ActorState.STOPPED

    main_host._install_packages = install_then_stop

    async def scenario():
        await main_host._spawn_local_from_config(
            {"name": "d5", "type": "dynamic", "code": "x", "install": ["totally_missing_pkg_zzz"]},
            blocking_install=False,
        )
        await asyncio.gather(*main_host.detached)

    run(scenario())
    assert main_host.spawn_calls == []


def test_no_packages_means_nothing_to_install(main_host):
    outcome = run(main_host._install_packages([], agent_name="d9"))
    assert outcome.ok
    assert main_host.sent == []


def test_without_an_installer_the_install_is_reported_unavailable(main_host):
    outcome = run(main_host._install_packages(["totally_missing_pkg_zzz"], agent_name="d10"))
    assert outcome.unavailable


def test_an_installer_that_never_answers_times_out(main_host, monkeypatch):
    main_host._registry.add(FakeActor("installer"))
    monkeypatch.setattr(spawning, "install_wait_s", lambda _count: 0.05)

    async def silent_send(target_id, msg_type, payload):
        main_host.sent.append(payload)  # taken in, never answered

    main_host.send = silent_send
    outcome = run(main_host._install_packages(["totally_missing_pkg_zzz"], agent_name="d11"))
    assert outcome.timed_out
    assert main_host._result_futures == {}


def test_an_installer_with_no_room_is_not_waited_for(main_host, monkeypatch):
    main_host._registry.add(FakeActor("installer"))
    monkeypatch.setattr(spawning, "install_wait_s", lambda _count: 600.0)

    async def refused_send(target_id, msg_type, payload):
        return False  # its mailbox was full

    main_host.send = refused_send
    outcome = run(main_host._install_packages(["totally_missing_pkg_zzz"], agent_name="d12"))
    assert outcome.busy
    assert not outcome.ok
    assert main_host._result_futures == {}


# ── A name the state directory refuses ───────────────────────────────────────


@pytest.mark.parametrize("name", ["..", ".", "../outside", "..hidden"])
def test_a_name_that_would_climb_out_of_the_state_directory_is_not_spawned(main_host, name, caplog):
    # An agent's name is also the directory its state is kept in. Such a name
    # was refused only when the agent was built, after a state shipped with a
    # migration had been written under it -- and then quietly gone.
    applied = []

    async def _apply(agent_name, config):
        applied.append(agent_name)

    main_host._apply_initial_state = _apply
    config = {"name": name, "type": "llm", "_initial_state": {"conversation_history": ["x"]}}

    actor = run(main_host._spawn_local_from_config(config))

    assert actor is None
    assert main_host.spawn_calls == []
    assert applied == [], "nothing is written for an agent that will not exist"
    assert "unsafe agent name" in caplog.text


def test_a_migrated_state_is_applied_through_the_stores(main_host, tmp_path, monkeypatch):
    db, store = WactorzDB(tmp_path / "wactorz.db"), PickleStore(str(tmp_path))
    monkeypatch.setattr(spawning, "get_db", lambda: db)
    monkeypatch.setattr(spawning, "get_pickle_store", lambda: store)
    config = {"name": "mover", "_initial_state": {"conversation_history": ["x"], "count": 3}}

    run(main_host._apply_initial_state("mover", config))

    assert "_initial_state" not in config, "the snapshot is not kept in the spawn registry"
    assert db.kv_get("mover", "conversation_history") == ["x"]
    assert store.load("mover") == {"count": 3}
