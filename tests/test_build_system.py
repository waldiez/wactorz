# SPDX-License-Identifier: Apache-2.0
"""What the start supervises, in what order, and what each factory builds.

``build_system`` is run with the broker, the stores and the agents' classes
replaced, so what it decides is all that is left to look at: which agents a
profile starts, which of a deployment's agents main or a node runs instead, the
agents a pipeline is made of, and each pipeline's record beside main's rules.
"""

import argparse
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz import app as app_mod
from wactorz.core.persistence import maintenance


class _Supervisor:
    def __init__(self) -> None:
        #: (name, factory, max_restarts, restart_delay), in the order supervised.
        self.supervised: list[tuple[str, Any, int, float]] = []
        self.started = False

    def supervise(self, name: str, factory: Any, **kwargs: Any) -> "_Supervisor":
        self.supervised.append((name, factory, kwargs["max_restarts"], kwargs["restart_delay"]))
        return self

    async def start(self) -> None:
        self.started = True

    @property
    def names(self) -> list[str]:
        return [name for name, *_ in self.supervised]


class _System:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.supervisor = _Supervisor()
        self.registry = SimpleNamespace()
        self._mqtt_client: Any = None
        self.topic_bus: Any = None


class _Built:
    """Stands in for every agent class: keeps what it was built with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        self.name = kwargs.get("name", "monitor")
        self._persistence_api: Any = None


def _plugin(name: str, autostart: bool = True) -> Any:
    return SimpleNamespace(
        name=name, autostart=autostart, build=lambda **kwargs: _Built(name=name, **kwargs)
    )


class _Main:
    def __init__(self, known: tuple[str, ...] = ()) -> None:
        self.known = dict.fromkeys(known)
        self.saved: list[dict[str, Any]] = []

    def get_pipeline_rules(self) -> dict[str, Any]:
        return self.known

    def save_pipeline_rule(self, record: dict[str, Any]) -> None:
        self.saved.append(record)


def _pipe(name: str, steps: tuple[str, ...], *, schedule: bool = False, rules: int = 0) -> Any:
    rule_names = tuple(f"{name}-rule-{i}" for i in range(rules))
    schedule_name = f"{name}-schedule"
    return SimpleNamespace(
        name=name,
        steps=tuple(_plugin(step) for step in steps),
        schedule={"every": 60} if schedule else None,
        tick_topic=f"pipelines/{name}/tick",
        schedule_name=schedule_name,
        rules=tuple({"rule": i} for i in range(rules)),
        rule_names=rule_names,
        agent_names=(*steps, *((schedule_name,) if schedule else ()), *rule_names),
        record=lambda: {"pipeline": name},
    )


class _World:
    """What the start reads, set by each test before it runs."""

    def __init__(self) -> None:
        self.home_assistant = False
        self.main: _Main | None = _Main()
        self.plugins: dict[str, Any] = {}
        self.pipelines: dict[str, Any] = {}
        self.recorded: dict[str, str] = {}
        self.web_ui: list[dict[str, Any]] = []
        self.order: list[str] = []


@pytest.fixture(name="world")
def world_fixture(monkeypatch: pytest.MonkeyPatch) -> _World:
    world = _World()
    for name in (
        "MainActor",
        "MonitorActor",
        "InstallerAgent",
        "HomeAssistantAgent",
        "HomeAssistantMapAgent",
        "HomeAssistantStateBridgeAgent",
        "CatalogAgent",
        "ScheduledAgent",
        "RuleAgent",
    ):
        monkeypatch.setattr(app_mod, name, _Built)
    monkeypatch.setattr(app_mod, "ActorSystem", _System)

    async def _provider(_args: Any, _minimal: bool) -> str:
        return "the-provider"

    async def _create(*_args: Any, **_kwargs: Any) -> str:
        return "mqtt"

    async def _web_ui(**kwargs: Any) -> None:
        world.web_ui.append(kwargs)
        world.order.append("web ui")

    def _orchestrator(_system: Any, _main: Any, requested: Any = None) -> str:
        world.order.append("orchestrator")
        return f"orchestrator:{requested}"

    monkeypatch.setattr(app_mod, "_build_provider", _provider)
    monkeypatch.setattr(app_mod, "ensure_state_dir", lambda: "/state")
    monkeypatch.setattr(app_mod.MQTTPublisher, "create", _create)
    monkeypatch.setattr(app_mod, "init_topic_bus", lambda **_kw: "bus")
    monkeypatch.setattr(app_mod, "init_persistence", lambda **_kw: ("db", "store"))
    monkeypatch.setattr(app_mod, "PersistenceAPI", lambda db, store, name: (db, store, name))
    monkeypatch.setattr(app_mod, "provider_for", lambda role, provider: f"{role}:{provider}")
    monkeypatch.setattr(app_mod, "home_assistant_agents_enabled", lambda _c: world.home_assistant)
    monkeypatch.setattr(app_mod, "stored_spawn_registry", lambda _db: None)
    monkeypatch.setattr(app_mod, "placed_by", lambda _r: world.recorded)
    monkeypatch.setattr(app_mod.plugins, "discover", lambda: world.plugins)
    monkeypatch.setattr(app_mod.pipelines, "discover", lambda: world.pipelines)
    monkeypatch.setattr(app_mod, "_start_web_ui", _web_ui)
    monkeypatch.setattr(maintenance, "register", lambda *_a: world.order.append("maintenance"))
    monkeypatch.setattr(maintenance, "start", lambda: None)
    monkeypatch.setattr(app_mod, "find_main_actor", lambda _r: world.main)
    monkeypatch.setattr(app_mod, "install_orchestrator", _orchestrator)
    monkeypatch.setattr(app_mod, "_current_system", None)
    # Whatever the environment running the tests says, the profile is the arguments'.
    monkeypatch.setattr(app_mod, "CONFIG", dataclasses.replace(app_mod.CONFIG, minimal=False))
    return world


def _args(**overrides: Any) -> argparse.Namespace:
    fields: dict[str, Any] = {
        "mqtt_broker": "broker.test",
        "mqtt_port": 1884,
        "monitor_port": 8888,
        "minimal": False,
        "no_monitor": False,
        "orchestrator": None,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


async def _start(**overrides: Any) -> _System:
    system, _main, _db, _orchestrator = await app_mod.build_system(_args(**overrides))
    assert isinstance(system, _System)
    return system


class TestTheBuiltIns:
    async def test_the_full_profile_starts_them_all_in_order(self, world: _World) -> None:
        world.home_assistant = True

        system = await _start()

        assert system.supervisor.names == [
            "main",
            "monitor",
            "installer",
            "home-assistant-agent",
            "home-assistant-map-agent",
            "home-assistant-state-bridge",
            "catalog",
        ]
        restarts = {name: (most, delay) for name, _f, most, delay in system.supervisor.supervised}
        assert restarts["main"] == (10, 2.0)
        assert restarts["installer"] == (3, 2.0)
        assert restarts["home-assistant-agent"] == (5, 1.0)

    async def test_without_home_assistant_its_agents_are_left_out(self, world: _World) -> None:
        system = await _start()

        assert system.supervisor.names == ["main", "monitor", "installer", "catalog"]

    async def test_the_minimal_profile_starts_the_monitor_alone(self, world: _World) -> None:
        world.main = None

        system = await _start(minimal=True)

        assert system.supervisor.names == ["monitor"]

    async def test_main_that_did_not_start_stops_the_start(self, world: _World) -> None:
        world.main = None

        with pytest.raises(app_mod.MainDidNotStartError):
            await _start()

    async def test_each_factory_builds_its_agent_with_persistence(self, world: _World) -> None:
        system = await _start()

        built = {name: factory() for name, factory, *_ in system.supervisor.supervised}
        assert built["main"].kwargs["llm_provider"] == "main:the-provider"
        assert built["main"]._persistence_api == ("db", "store", "main")
        assert built["catalog"].kwargs["persistence_dir"] == "/state"
        # Fresh on every call, as a restart needs.
        main_factory = system.supervisor.supervised[0][1]
        assert main_factory() is not main_factory()


class TestADeploymentsAgents:
    async def test_one_main_or_a_node_runs_is_not_supervised_here(self, world: _World) -> None:
        world.plugins = {
            name: _plugin(name, autostart)
            for name, autostart in [
                ("fresh", True),
                ("moved", True),
                ("restored", True),
                ("lazy", False),
            ]
        }
        world.recorded = {"moved": "edge", "restored": ""}

        system = await _start()

        assert system.supervisor.names[-1] == "fresh"
        assert {"moved", "restored", "lazy"}.isdisjoint(system.supervisor.names)
        plugin_agent = system.supervisor.supervised[-1][1]()
        assert plugin_agent.kwargs["llm_provider"] == "dynamic:the-provider"


class TestAPipeline:
    async def test_its_steps_schedule_and_rules_are_each_supervised(self, world: _World) -> None:
        world.pipelines = {"p": _pipe("p", ("p-step", "p-moved"), schedule=True, rules=2)}
        world.recorded = {"p-moved": "edge"}

        system = await _start()

        assert system.supervisor.names[-4:] == ["p-step", "p-schedule", "p-rule-0", "p-rule-1"]
        built = {name: factory() for name, factory, *_ in system.supervisor.supervised}
        assert built["p-schedule"].kwargs["publish_topic"] == "pipelines/p/tick"
        assert built["p-rule-1"].args == ({"rule": 1},)

    async def test_it_is_recorded_beside_mains_rules_once(self, world: _World) -> None:
        main = _Main(known=("old",))
        world.main = main
        world.pipelines = {"old": _pipe("old", ("o",)), "new": _pipe("new", ("n",))}

        await _start()

        assert main.saved == [{"pipeline": "new"}]


class TestTheOrderOfTheStart:
    async def test_the_web_ui_is_up_before_the_agents_start(self, world: _World) -> None:
        system = await _start(orchestrator="direct")

        assert world.order == ["web ui", "maintenance", "orchestrator"]
        assert system.supervisor.started
        assert world.web_ui[0]["persistence_db"] == "db"

    async def test_without_the_monitor_there_is_no_web_ui(self, world: _World) -> None:
        await _start(no_monitor=True)

        assert world.web_ui == []
