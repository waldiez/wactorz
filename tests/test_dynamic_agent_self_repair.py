"""What happens when the model repairs a crashing agent's code at run time.

A repaired program must be the one that runs afterwards. Both repair paths
recompile into a fresh namespace, and a loop that keeps calling the function
it was handed before the recompile runs the crash forever while reporting
that a fix was applied.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import ActorState

CRASHING_PROCESS = """
count = 0

async def setup(agent):
    agent.state["setup_runs"] = agent.state.get("setup_runs", 0) + 1

async def process(agent):
    global count
    count += 1
    raise RuntimeError("Intentional crash: reached count of 5")
"""

# The repaired process() reads a name only its own setup() defines, so it
# runs cleanly only when the repaired program was started from setup().
REPAIRED_PROCESS = """
threshold = None

async def setup(agent):
    global threshold
    threshold = 5
    agent.state["setup_runs"] = agent.state.get("setup_runs", 0) + 1

async def process(agent):
    agent.state["repaired_runs"] = agent.state.get("repaired_runs", 0) + threshold
"""

CRASHING_SETUP = """
async def setup(agent):
    raise ValueError("cannot open the camera")

async def process(agent):
    agent.state["process_runs"] = agent.state.get("process_runs", 0) + 1
"""

REPAIRED_SETUP = """
async def setup(agent):
    agent.state["repaired_setup"] = True

async def process(agent):
    agent.state["process_runs"] = agent.state.get("process_runs", 0) + 1
"""


class ScriptedLLM:
    """Answers each repair request with the next scripted program."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, messages: Any, system: str = "", max_tokens: int = 0) -> Any:
        self.prompts.append(messages[-1]["content"])
        answer = self.answers.pop(0) if self.answers else "async def process(agent):\n    pass\n"
        return answer, {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop sleeps for seconds between errors; the tests should not."""
    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("wactorz.agents.dynamic.agent.asyncio.sleep", instant)


def make_agent(tmp_path: Path, code: str, llm: Any) -> DynamicAgent:
    agent = DynamicAgent(
        name="crashy",
        code=code,
        poll_interval=0,
        llm_provider=llm,
        persistence_dir=str(tmp_path),
    )
    agent.state = ActorState.RUNNING
    return agent


async def until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0)


async def shut_down(agent: DynamicAgent) -> None:
    agent.state = ActorState.STOPPED
    for task in agent._program_tasks:
        task.cancel()
    await asyncio.gather(*agent._program_tasks, return_exceptions=True)


class TestRepairingProcess:
    async def test_the_repaired_program_is_what_runs_afterwards(self, tmp_path: Path) -> None:
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 3)
        await shut_down(agent)

        assert len(llm.prompts) == 1
        assert agent._code == REPAIRED_PROCESS.strip()
        assert agent.state is not ActorState.FAILED
        assert agent._consecutive_errors == 0

    async def test_the_repaired_program_starts_from_its_own_setup(self, tmp_path: Path) -> None:
        """Module state the old setup() built is gone with the old namespace."""
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        await shut_down(agent)

        assert agent._api.state["setup_runs"] == 2

    async def test_the_old_loop_ends_instead_of_running_beside_the_new_one(
        self, tmp_path: Path
    ) -> None:
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        crashes_after_repair = agent.metrics.errors
        for _ in range(20):
            await asyncio.sleep(0)
        await shut_down(agent)

        assert agent.metrics.errors == crashes_after_repair
        assert sum(1 for task in agent._program_tasks if not task.done()) == 0

    async def test_the_old_programs_subscriptions_are_cancelled(self, tmp_path: Path) -> None:
        """A listener would keep calling a callback from the replaced namespace."""
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()
        listener = asyncio.create_task(asyncio.Event().wait())
        agent._track_program_task(listener)
        agent._subscribed_topics[("sensors/temp", 1)] = object()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        await shut_down(agent)

        assert listener.cancelled()
        assert agent._subscribed_topics == {}

    async def test_a_second_round_tells_the_model_the_first_fix_failed(
        self, tmp_path: Path
    ) -> None:
        llm = ScriptedLLM(CRASHING_PROCESS, REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        await shut_down(agent)

        assert len(llm.prompts) == 2
        assert "Intentional crash" not in llm.prompts[0].split("Traceback")[0].split("Error:")[0]
        assert "repaired automatically" in llm.prompts[1]
        assert "RuntimeError: Intentional crash: reached count of 5" in llm.prompts[1]

    async def test_repair_rounds_are_bounded_and_then_the_supervisor_takes_over(
        self, tmp_path: Path
    ) -> None:
        llm = ScriptedLLM(CRASHING_PROCESS, CRASHING_PROCESS, REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent.state is ActorState.FAILED)
        await shut_down(agent)

        assert len(llm.prompts) == DynamicAgent._MAX_PROCESS_FIX_ROUNDS
        assert "repaired_runs" not in agent._api.state

    async def test_a_clean_run_restores_the_repair_budget(self, tmp_path: Path) -> None:
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        await shut_down(agent)

        assert agent._process_fix_rounds == 0
        assert agent._process_fix_errors == []

    async def test_a_fix_that_does_not_compile_leaves_the_program_running(
        self, tmp_path: Path
    ) -> None:
        llm = ScriptedLLM("async def process(agent)\n    pass\n", REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("repaired_runs", 0) >= 1)
        await shut_down(agent)

        assert len(llm.prompts) == 2
        assert agent._code == REPAIRED_PROCESS.strip()

    async def test_the_prompt_carries_the_agents_purpose(self, tmp_path: Path) -> None:
        llm = ScriptedLLM(REPAIRED_PROCESS)
        agent = make_agent(tmp_path, CRASHING_PROCESS, llm)
        agent.description = "Counts to five and reports each step"
        await agent.on_start()

        await until(lambda: len(llm.prompts) >= 1)
        await shut_down(agent)

        assert "Counts to five and reports each step" in llm.prompts[0]


class TestRepairingSetup:
    async def test_the_repaired_setup_is_the_one_retried(self, tmp_path: Path) -> None:
        llm = ScriptedLLM(REPAIRED_SETUP)
        agent = make_agent(tmp_path, CRASHING_SETUP, llm)
        await agent.on_start()

        await until(lambda: agent._api.state.get("process_runs", 0) >= 1)
        await shut_down(agent)

        assert agent._api.state.get("repaired_setup") is True
        assert agent.state is not ActorState.FAILED
        assert agent._code == REPAIRED_SETUP.strip()
