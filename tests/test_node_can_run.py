"""Which agent configs a node can run.

A node runs every agent as generated code: what the config carries, or the
bridge main writes for an LLM agent. Every other kind is a class built into the
server, whose config carries no program.
"""

import pytest

from wactorz.agents.main.spawns import why_a_node_cannot_run


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


@pytest.mark.parametrize("agent_type", ["native", "ha_actuator", "scheduled", "rule", "module"])
def test_a_built_in_kind_cannot_and_says_which(agent_type: str) -> None:
    reason = why_a_node_cannot_run({"name": "x", "type": agent_type})

    assert reason is not None
    assert f"{agent_type} agent" in reason


def test_a_config_with_no_program_cannot() -> None:
    # A system prompt alone runs as an LLM agent on main, but only an explicit
    # `type: llm` gets the code a node needs.
    assert why_a_node_cannot_run({"name": "helper", "system_prompt": "Be brief."})
    assert why_a_node_cannot_run({"name": "empty", "code": "   "})
