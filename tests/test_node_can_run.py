"""Which agent configs a node can run.

A node runs an agent as generated code: what the config carries, or the bridge
main writes for an LLM agent. A module agent it builds from a package installed
there, so one goes only to a node that says it has the target. Every other kind
is a class built into the server, whose config carries no program.
"""

import pytest

from wactorz.agents.main.spawns import why_a_node_cannot_run

TARGET = "imu_anomaly.agent:detect"


@pytest.mark.parametrize(
    "config",
    [
        {"name": "collector", "code": "async def process(agent):\n    pass\n"},
        {"name": "collector", "type": "dynamic", "code": "x = 1"},
        {"name": "helper", "type": "llm", "system_prompt": "Be brief."},
        {"name": "helper", "type": "LLM"},
    ],
)
def test_code_or_an_llm_agent_can_run(config: dict) -> None:
    assert why_a_node_cannot_run(config) is None


@pytest.mark.parametrize("agent_type", ["native", "ha_actuator", "scheduled", "rule"])
def test_a_built_in_kind_cannot_and_says_which(agent_type: str) -> None:
    reason = why_a_node_cannot_run({"name": "x", "type": agent_type})

    assert reason is not None
    assert f"{agent_type} agent" in reason


def test_a_config_with_no_program_cannot() -> None:
    # A system prompt alone runs as an LLM agent on main, but only an explicit
    # `type: llm` gets the code a node needs.
    assert why_a_node_cannot_run({"name": "helper", "system_prompt": "Be brief."})
    assert why_a_node_cannot_run({"name": "empty", "code": "   "})


class TestAModuleAgent:
    """Built from a package on the node, so only a node that has it may run it."""

    def test_it_can_run_where_the_node_reports_its_target(self) -> None:
        config = {"name": "imu-anomaly", "type": "module", "target": TARGET}

        assert why_a_node_cannot_run(config, [TARGET, "other.pkg:agent"]) is None

    def test_a_node_that_has_not_reported_it_is_refused_with_the_way_to_fix_it(self) -> None:
        config = {"name": "imu-anomaly", "type": "module", "target": TARGET}

        reason = why_a_node_cannot_run(config, ["other.pkg:agent"])

        assert reason is not None
        assert TARGET in reason
        assert "WACTORZ_AGENTS" in reason

    def test_a_node_that_has_said_nothing_is_refused(self) -> None:
        assert why_a_node_cannot_run({"name": "x", "type": "module", "target": TARGET})

    def test_one_naming_no_target_is_refused_whatever_the_node_has(self) -> None:
        assert why_a_node_cannot_run({"name": "x", "type": "module"}, [TARGET])
