"""The planner is told which machines there are, and how an agent reads its own.

A request such as "monitor the CPU on raspberrypi" names a machine and asks for
something that machine measures about itself. The planner could not place an
agent on a node it was never told about, and it went looking for an MQTT topic
or a Home Assistant entity that publishes the CPU. Finding none, it planned an
agent whose only job was to ask the operator for one. These tests pin what it
is told instead: the nodes, what each machine is and whether it takes an agent
now, that an agent reads its machine with psutil, and that a plan which cannot
be built says so rather than spawning something to ask.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner import context, nodes
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.context import unresolved
from wactorz.agents.planner.nodes import NO_NODES, machine_of, node_line, nodes_section
from wactorz.agents.prompts.planner_prompts import NODE_PLACEMENT_RULES
from wactorz.config import CONFIG, DeployTarget

#: The Pi's manifest, as its runner reports it.
PI = {"model": "Raspberry Pi 4 Model B", "arch": "aarch64", "cpu_count": 4, "ram_total_mb": 3796}


def _main(
    listed: list[dict[str, Any]], *, refusing: dict[str, str] | None = None, model_node: str = ""
) -> Any:
    """Main as the NODES section reads it: its node table and where model code goes."""
    return SimpleNamespace(
        placement=SimpleNamespace(model_code_node=lambda: model_node),
        nodes=SimpleNamespace(
            list_nodes=lambda: listed,
            refuses_tasks=lambda name: (refusing or {}).get(name),
            known={str(info["node"]): info for info in listed},
        ),
    )


@pytest.fixture(autouse=True)
def _no_deploy_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from no configured targets, whatever this machine's `.env` names."""
    for module in (nodes, context):
        monkeypatch.setattr(module, "CONFIG", dataclasses.replace(CONFIG, deploy_targets=()))


class TestTheNodesSection:
    def test_without_main_every_agent_runs_on_main(self) -> None:
        assert nodes_section(None) == NO_NODES

    def test_a_node_says_what_its_machine_is_and_what_it_runs(self) -> None:
        main = _main(
            [{"node": "raspberrypi", "online": True, "agents": ["camera"], "manifest": PI}]
        )

        line = nodes_section(main)

        assert line.startswith("- raspberrypi: Raspberry Pi 4 Model B, aarch64, 4 CPUs")
        assert "3796 MB RAM" in line
        assert "runs camera" in line

    def test_the_node_model_code_goes_to_says_so(self) -> None:
        main = _main([{"node": "sandbox", "online": True, "agents": []}], model_node="sandbox")

        assert "where a dynamic agent naming no node goes" in nodes_section(main)

    def test_a_node_main_would_not_hear_from_says_it_cannot_take_agents_and_why(self) -> None:
        main = _main(
            [{"node": "sandbox", "online": True, "agents": []}],
            refusing={"sandbox": "deploy it again: /deploy sandbox"},
        )

        assert "CANNOT TAKE AGENTS NOW: deploy it again: /deploy sandbox" in nodes_section(main)

    def test_an_offline_node_cannot_take_agents(self) -> None:
        main = _main([{"node": "raspberrypi", "online": False, "agents": []}])

        assert "offline — cannot take agents now" in nodes_section(main)

    def test_a_deploy_target_never_heard_from_is_listed_as_not_deployed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            nodes,
            "CONFIG",
            dataclasses.replace(CONFIG, deploy_targets=(DeployTarget(name="garage"),)),
        )

        assert "- garage: configured, never heard from" in nodes_section(_main([]))

    def test_a_machine_that_has_not_said_what_it_is_is_described_by_nothing(self) -> None:
        assert machine_of({}) == ""

    def test_its_devices_are_named(self) -> None:
        assert machine_of({"devices": ["camera", "gpio"]}) == "devices: camera gpio"

    def test_one_line_reads_without_a_manifest(self) -> None:
        line = node_line("pc", {"online": True, "agents": []}, refusal=None, takes_model_code=False)

        assert line == "- pc: has not said what machine it is"


class TestWhatThePlannerIsToldAboutPlacing:
    def test_it_is_told_to_read_the_machine_with_psutil(self) -> None:
        assert "psutil.cpu_percent(interval=None)" in NODE_PLACEMENT_RULES

    def test_it_is_told_to_name_only_a_node_that_takes_agents(self) -> None:
        assert "never one marked CANNOT TAKE AGENTS or offline" in NODE_PLACEMENT_RULES


class TestARequestWithNoSourceFound:
    def test_a_disk_reading_is_read_on_the_machine(self) -> None:
        _enriched, note = unresolved("watch the disk", ["disk", "storage"])

        assert note.startswith("Read on the machine itself")

    def test_a_temperature_is_searched_for_since_it_may_be_a_sensor_s(self) -> None:
        _enriched, note = unresolved("log the temperature", ["temperature", "temp", "thermal"])

        assert note.startswith("No data source found")

    def test_a_machine_reading_is_read_on_the_machine(self) -> None:
        enriched, note = unresolved("monitor the cpu", ["cpu", "processor"])

        assert "psutil" in enriched
        assert note == "Read on the machine itself: cpu, processor."

    def test_the_model_is_never_told_to_plan_an_agent_that_asks(self) -> None:
        enriched, _note = unresolved("tell me when it floods", ["water", "flood", "leak"])

        assert "Ask the user" not in enriched
        assert "instead of planning an agent to ask" in enriched


def _planner(tmp_path: Path) -> PlannerAgent:
    planner = PlannerAgent(
        llm_provider=FakeProvider(script={"USER REQUEST": "[]"}),
        persistence_dir=str(tmp_path),
        auto_terminate=False,
    )
    planner._registry = SimpleNamespace(find_by_name=lambda _name: None, all_actors=list)  # pyright: ignore[reportAttributeAccessIssue]
    return planner


class TestResolvingAMachinesOwnReading:
    async def test_a_named_node_s_cpu_is_read_there_not_looked_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Home Assistant's own processor sensor measures Home Assistant's host,
        # not the Pi the request names.
        main = _main([{"node": "raspberrypi", "online": True, "agents": []}])
        monkeypatch.setattr(context, "find_main_actor", lambda _registry: main)
        planner = _planner(tmp_path)

        async def _never() -> list[dict[str, Any]]:
            raise AssertionError("Home Assistant was searched for a machine's own reading")

        planner._fetch_ha_entities = _never  # pyright: ignore[reportAttributeAccessIssue]

        enriched, note = await planner._resolve_data_references(
            "spawn a CPU monitor agent on raspberrypi"
        )

        assert "psutil" in enriched
        assert note.startswith("Read on the machine itself")


class TestThePipelinePrompt:
    async def test_it_lists_the_nodes_and_how_to_place_an_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _main([{"node": "raspberrypi", "online": True, "agents": [], "manifest": PI}])
        monkeypatch.setattr("wactorz.agents.planner.pipeline.find_main_actor", lambda _r: main)
        planner = _planner(tmp_path)

        async def _empty_pair(*_args: Any) -> tuple[str, str]:
            return "", ""

        async def _empty(*_args: Any) -> str:
            return ""

        planner._gather_topic_bus_context = _empty_pair  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_notification_urls = _empty  # pyright: ignore[reportAttributeAccessIssue]
        planner._prompt_fragments = ()

        await planner._decompose_pipeline("monitor the cpu on raspberrypi", [])

        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert "═══ NODES (the machines an agent can run on) ═══" in prompt
        assert "- raspberrypi: Raspberry Pi 4 Model B" in prompt
        assert NODE_PLACEMENT_RULES in prompt


class TestAPlanThatCannotBeBuilt:
    async def test_the_model_saying_so_reaches_the_user_and_spawns_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.agents.planner.pipeline.find_main_actor", lambda _r: None)
        planner = PlannerAgent(
            llm_provider=FakeProvider(
                script={"USER REQUEST": '[{"_feasibility_error": "No node named garage."}]'}
            ),
            persistence_dir=str(tmp_path),
            auto_terminate=False,
            prompt_fragments=(),
        )
        planner._registry = SimpleNamespace(find_by_name=lambda _name: None, all_actors=list)  # pyright: ignore[reportAttributeAccessIssue]

        async def _empty_pair(*_args: Any) -> tuple[str, str]:
            return "", ""

        async def _empty(*_args: Any) -> str:
            return ""

        async def _nothing_spawned(_config: dict[str, Any]) -> None:
            raise AssertionError("an agent was spawned for a plan that cannot be built")

        planner._gather_topic_bus_context = _empty_pair  # pyright: ignore[reportAttributeAccessIssue]
        planner._gather_notification_urls = _empty  # pyright: ignore[reportAttributeAccessIssue]
        planner._spawn_agent = _nothing_spawned  # pyright: ignore[reportAttributeAccessIssue]

        said = await planner._run_pipeline("log the garage's cpu every minute", [])

        assert said == "Cannot set up this pipeline:\n\nNo node named garage."
