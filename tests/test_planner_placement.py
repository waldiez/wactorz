"""The planner places the code it writes where main places the model's code.

A plan is the model's, like a ``<spawn>`` block, so its agents go where main's
placement says (`wactorz.agents.main.placement`): to the node set for
model-written code, or on main only where that is allowed. One sent to a node is
waited for until the node reports it running, because the plan's next step may
ask it; it is found and asked through main, which the planner, holding only its
own process's registry, could not do by itself. Saved for a restart, it keeps
its node.

Without ``WACTORZ_MODEL_CODE_NODE`` the planner spawns here, as it always has.
"""

import dataclasses
import time
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.main import placement
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import DelegationManager
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.placement import Placement
from wactorz.agents.mixins import PlacedOnNode
from wactorz.agents.planner import execution as execution_mod
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.config import DeployTarget
from wactorz.core.registry import ActorRegistry

NODE = "sandbox"


def dynamic(name: str = "watcher", **over: Any) -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": "async def setup(agent): pass", **over}


class _Delegation:
    """Main's delegation, recording what it was asked to carry to a node."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def delegate_payload(
        self, name: str, payload: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        self.asked.append((name, payload))
        return {"result": f"{name} answered"}


class _Registry:
    """The planner's registry: main, and nothing else in this process."""

    def __init__(self, main: MainActor) -> None:
        self.main = main

    def find_by_name(self, name: str) -> Any:
        return self.main if name == "main" else None


class Setup:
    """A planner beside main, with what each was asked to spawn recorded."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.placement = Placement(main)
        self.delegation = _Delegation()
        main.delegation = cast(
            "DelegationManager", self.delegation
        )  # a fake standing in for main's
        self.main = main
        #: Whether the node reports what it is sent, in the heartbeat that follows.
        self.node_reports = True
        self.sent: list[dict[str, Any]] = []
        self.here: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []

        async def _sent(config: dict[str, Any], save: bool = True, **_kw: Any) -> PlacedOnNode:
            self.sent.append(config)
            if self.node_reports:
                self.report(config["name"])
            return PlacedOnNode(config["name"], config["node"], True)

        monkeypatch.setattr(main, "_spawn_from_config", _sent)
        monkeypatch.setattr(main, "_save_to_spawn_registry", self.saved.append)

        planner = PlannerAgent(
            llm_provider=FakeProvider(script={}),
            persistence_dir=str(tmp_path),
            auto_terminate=False,
        )
        planner._registry = cast("ActorRegistry", _Registry(main))  # a fake standing in for it

        async def _here(config: dict[str, Any], **_kw: Any) -> Any:
            self.here.append(config)
            return main  # any actor will do: the planner only reads its name

        async def _quiet(_message: str) -> None:
            return None

        monkeypatch.setattr(planner, "_spawn_local_from_config", _here)
        monkeypatch.setattr(planner, "_log", _quiet)
        self.planner = planner

    def report(self, *agents: str) -> None:
        """The node's heartbeat, listing ``agents``."""
        self.main.nodes.known[NODE] = {"last_seen": time.time(), "agents": list(agents)}


@pytest.fixture(name="setup")
def setup_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    return Setup(tmp_path, monkeypatch)


@pytest.fixture(name="sandboxed")
def sandboxed_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(placement, "MODEL_CODE_NODE", NODE)
    monkeypatch.setattr(placement, "MODEL_CODE_ON_MAIN", "never")
    monkeypatch.setattr(
        placement,
        "CONFIG",
        dataclasses.replace(placement.CONFIG, deploy_targets=(DeployTarget(name=NODE),)),
    )


class TestUnset:
    async def test_the_planner_spawns_here_as_it_always_has(
        self, setup: Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(placement, "MODEL_CODE_NODE", "")

        await setup.planner._spawn_agent(dynamic())

        assert [c["name"] for c in setup.here] == ["watcher"]
        assert setup.sent == []


@pytest.mark.usefixtures("sandboxed")
class TestSpawning:
    async def test_model_code_goes_to_the_node(self, setup: Setup) -> None:
        placed = await setup.planner._spawn_agent(dynamic())

        assert placed == PlacedOnNode("watcher", NODE, True)
        assert [c["node"] for c in setup.sent] == [NODE]
        assert setup.here == []

    async def test_what_runs_main_s_own_classes_stays_here(self, setup: Setup) -> None:
        await setup.planner._spawn_agent(
            {"name": "summariser", "type": "llm", "system_prompt": "be brief"}
        )

        assert [c["name"] for c in setup.here] == ["summariser"]
        assert setup.sent == []

    async def test_a_refused_spawn_spawns_nothing(self, setup: Setup) -> None:
        # WACTORZ_MODEL_CODE_ON_MAIN=never: a plan asking for main gets nowhere.
        placed = await setup.planner._spawn_agent(dynamic(node="main"))

        assert placed is None
        assert setup.here == []
        assert setup.sent == []

    async def test_it_waits_for_the_node_to_report_the_agent(
        self, setup: Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The next step may ask it, and asking needs main to know where it is.
        setup.node_reports = False
        monkeypatch.setattr(execution_mod, "NODE_START_S", 0.2)
        monkeypatch.setattr(execution_mod, "NODE_POLL_S", 0.01)

        placed = await setup.planner._spawn_agent(dynamic())

        assert placed is None
        result = setup.planner._spawn_results["watcher"]
        assert result["status"] == "not_started_on_node"
        assert "did not report it running" in result["error"]

    async def test_the_plan_s_reply_says_why_a_step_was_not_spawned(self, setup: Setup) -> None:
        # Not only the log: the step's line in the reply carries the reason.
        step = {"name": "watcher", "spawn_config": dynamic(node="main")}

        outcome = await setup.planner._spawn_pipeline_step(step, "watch the door")

        assert outcome.wired is not None
        assert "failed to spawn: " in outcome.wired
        assert "WACTORZ_MODEL_CODE_ON_MAIN=never" in outcome.wired

    async def test_an_agent_on_a_quiet_node_is_not_spawned_again(self, setup: Setup) -> None:
        # Gone quiet is not gone: a second copy would leave two of one name
        # when the node speaks again.
        setup.main.nodes.known[NODE] = {"last_seen": time.time() - 3600, "agents": ["watcher"]}
        plan = [{"step": 1, "agent": "watcher", "spawn_config": dynamic()}]

        await setup.planner._ensure_agents(plan)

        assert setup.sent == []

    async def test_an_agent_the_node_runs_is_not_spawned_again(self, setup: Setup) -> None:
        setup.report("watcher")
        plan = [{"step": 1, "agent": "watcher", "spawn_config": dynamic()}]

        await setup.planner._ensure_agents(plan)

        assert setup.sent == []
        assert setup.here == []


@pytest.mark.usefixtures("sandboxed")
class TestAskingIt:
    async def test_an_agent_on_a_node_is_asked_through_main(self, setup: Setup) -> None:
        setup.report("watcher")

        reply = await setup.planner._delegate_with_payload("watcher", {"text": "status?"})

        assert reply == {"result": "watcher answered"}
        assert setup.delegation.asked == [("watcher", {"text": "status?"})]

    async def test_one_nowhere_is_still_not_found(self, setup: Setup) -> None:
        reply = await setup.planner._delegate_with_payload("nobody", {"text": "status?"})

        assert reply == {"error": "Agent 'nobody' not found"}
        assert setup.delegation.asked == []


@pytest.mark.usefixtures("sandboxed")
class TestAPipelineStep:
    async def test_is_saved_with_its_node(self, setup: Setup) -> None:
        # Saved without it, a restart would bring the agent back on main.
        step = {"name": "watcher", "spawn_config": dynamic()}

        await setup.planner._spawn_pipeline_step(step, "watch the door")

        (saved,) = setup.saved
        assert saved["node"] == NODE
        assert saved["_rule"] is True
