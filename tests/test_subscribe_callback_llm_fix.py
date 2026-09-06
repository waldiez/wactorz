"""A failing subscribe callback is fixed by the LLM and survives the restart.

The fix is staged on the agent for the Supervisor restart, and persisted
state is keyed by agent name, so the restarted agent runs the fixed code on
the total the crashed one had reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wactorz.agents.dynamic.agent import DynamicAgent

BROKEN = """
def setup(agent):
    def on_person(payload):
        total = agent.recall("person_total", 0) + 1
        agent.persist("person_total", total)
        if total >= 10:
            1 / 0
    agent.subscribe("camera/livingroom/person", on_person)
"""

FIXED = """
def setup(agent):
    def on_person(payload):
        total = agent.recall("person_total", 0) + 1
        agent.persist("person_total", total)
    agent.subscribe("camera/livingroom/person", on_person)
"""


class FakeLLM:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    async def complete(self, messages: Any, system: Any = None, max_tokens: Any = None) -> Any:
        self.prompts.append(messages[-1]["content"])
        return self.raw, {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0}


def _agent(tmp_path: Path, llm: FakeLLM | None, code: str = BROKEN) -> DynamicAgent:
    return DynamicAgent(
        name="person-detection-counter",
        code=code,
        persistence_dir=str(tmp_path),
        llm_provider=llm,
    )


async def test_fix_is_staged_and_asks_to_keep_persisted_keys(tmp_path: Path) -> None:
    llm = FakeLLM(f"```python\n{FIXED}\n```")
    agent = _agent(tmp_path, llm)

    ok = await agent._fix_subscribe_callback_with_llm(
        ZeroDivisionError("division by zero"), "Traceback ...\nZeroDivisionError: division by zero"
    )

    assert ok is True
    assert agent._code.strip() == FIXED.strip()
    prompt = llm.prompts[0]
    assert "agent.subscribe() CALLBACK" in prompt
    assert "persist()/agent.recall() key" in prompt
    assert "division by zero" in prompt


async def test_unusable_fix_is_rejected_and_code_is_kept(tmp_path: Path) -> None:
    llm = FakeLLM("def setup(agent)\n    pass")  # syntax error
    agent = _agent(tmp_path, llm)

    ok = await agent._fix_subscribe_callback_with_llm(ZeroDivisionError(), "")

    assert ok is False
    assert agent._code == BROKEN


async def test_unsafe_fix_is_rejected(tmp_path: Path) -> None:
    llm = FakeLLM("import os\ndef setup(agent):\n    os.system('rm -rf /')\n")
    agent = _agent(tmp_path, llm)

    assert await agent._fix_subscribe_callback_with_llm(ZeroDivisionError(), "") is False
    assert agent._code == BROKEN


async def test_no_llm_means_no_fix(tmp_path: Path) -> None:
    agent = _agent(tmp_path, None)
    assert await agent._fix_subscribe_callback_with_llm(ZeroDivisionError(), "") is False


async def test_restarted_agent_continues_from_the_persisted_total(tmp_path: Path) -> None:
    # The crashed instance got the total to 15 before the callback failed.
    crashed = _agent(tmp_path, None)
    crashed.persist("person_total", 15)

    # The Supervisor restarts it under the same name with the fixed code.
    restarted = _agent(tmp_path, None, code=FIXED)
    await restarted._load_persistent_state()

    assert restarted.recall("person_total") == 15
