"""Generated code cannot take the process down with it.

An agent's program is written by a model, and a model asked for an agent that
stops after a while reaches for `sys.exit()` or `raise SystemExit` — there is
nothing else to reach for. Those derive from `BaseException`, so `except
Exception` never saw them, and asyncio re-raises `SystemExit` into the event
loop rather than storing it on the task: the loop ends and the process exits,
taking every other agent with it.

The four places a program runs are covered here: `process()`, `setup()`,
`handle_task()`, and a subscribe callback. Each treats a `SystemExit` as what it
is — a bug in that program — and routes it into the counting, reporting and
repair that any other error from the same place already gets. A node runs the
same programs in its own loop, which gets the same treatment.

The two that mean *stop* rather than *fail* still pass through: a cancellation,
which is how SIGTERM and SIGINT reach an agent here (`app.py` cancels the task
rather than raising), and a KeyboardInterrupt.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import ActorState
from wactorz.remote_runner import ProcessEscalated, _RemoteAgent

EXITING_PROCESS = """
async def process(agent):
    raise SystemExit("Lifecycle complete")
"""

EXITING_SETUP = """
async def setup(agent):
    raise SystemExit("Lifecycle complete")

async def process(agent):
    pass
"""

SYS_EXIT_PROCESS = """
import sys

async def process(agent):
    sys.exit("done")
"""

EXITING_HANDLE_TASK = """
async def process(agent):
    pass

async def handle_task(agent, payload):
    raise SystemExit("Lifecycle complete")
"""

WORKING_PROCESS = """
async def process(agent):
    agent.state['ticks'] = agent.state.get('ticks', 0) + 1
"""


class ScriptedLLM:
    """Answers every repair request with a program that works."""

    def __init__(self, answer: str = WORKING_PROCESS) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, messages: Any, system: str = "", max_tokens: int = 0) -> Any:
        self.prompts.append(messages[-1]["content"])
        return self.answer, {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


@pytest.fixture(autouse=True, name="no_backoff")
def no_backoff_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop backs off for seconds between errors; the tests should not."""
    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("wactorz.agents.dynamic.agent.asyncio.sleep", instant)


def make_agent(tmp_path: Path, code: str, llm: Any = None) -> DynamicAgent:
    agent = DynamicAgent(
        name="exiter",
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


class TestTheLoopSurvivesIt:
    """The event loop the agent runs on is still there afterwards."""

    async def test_a_process_that_exits_does_not_end_the_loop(self, tmp_path: Path) -> None:
        agent = make_agent(tmp_path, EXITING_PROCESS, ScriptedLLM())
        agent._compile_code()
        agent._start_program()

        # A second task standing in for every other agent in the process: it must
        # still be running once the exiting one has failed.
        alive = {"ticks": 0}

        async def bystander() -> None:
            while True:
                alive["ticks"] += 1
                await asyncio.sleep(0)

        other = asyncio.create_task(bystander())
        try:
            await until(lambda: agent.metrics.errors > 0)
            before = alive["ticks"]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert alive["ticks"] > before, "the loop stopped running other work"
        finally:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
            await shut_down(agent)

    async def test_sys_exit_is_treated_the_same_way(self, tmp_path: Path) -> None:
        """`sys.exit()` raises SystemExit; a model reaches for it just as readily."""
        agent = make_agent(tmp_path, SYS_EXIT_PROCESS, ScriptedLLM())
        agent._compile_code()
        agent._start_program()
        try:
            await until(lambda: agent.metrics.errors > 0)
        finally:
            await shut_down(agent)


class TestItIsTreatedAsTheProgramsBug:
    """Not swallowed into a log line — routed into the machinery that exists."""

    async def test_the_error_is_counted(self, tmp_path: Path) -> None:
        agent = make_agent(tmp_path, EXITING_PROCESS, ScriptedLLM())
        agent._compile_code()
        agent._start_program()
        try:
            await until(lambda: agent.metrics.errors >= 2)
        finally:
            await shut_down(agent)

    async def test_the_model_is_asked_to_repair_it(self, tmp_path: Path) -> None:
        llm = ScriptedLLM()
        agent = make_agent(tmp_path, EXITING_PROCESS, llm)
        agent._compile_code()
        agent._start_program()
        try:
            await until(lambda: bool(llm.prompts))
            assert "SystemExit" in llm.prompts[0] or "Lifecycle complete" in llm.prompts[0]
        finally:
            await shut_down(agent)

    async def test_a_setup_that_exits_fails_the_agent(self, tmp_path: Path) -> None:
        """setup() has its own repair budget; exhausting it marks the actor FAILED
        so the Supervisor takes over, which is what any other setup error does."""
        agent = make_agent(tmp_path, EXITING_SETUP, ScriptedLLM(EXITING_SETUP))
        agent._compile_code()
        agent._start_program()
        try:
            await until(lambda: agent.state == ActorState.FAILED, timeout=10.0)
        finally:
            await shut_down(agent)

    async def test_a_handle_task_that_exits_answers_the_caller(self, tmp_path: Path) -> None:
        """The sender is waiting on a reply; ending the process is not an answer,
        and neither is silence."""
        agent = make_agent(tmp_path, EXITING_HANDLE_TASK, ScriptedLLM())
        agent._compile_code()
        sent: list[Any] = []

        async def capture(target: str, kind: Any, payload: Any) -> None:
            sent.append((target, kind, payload))

        agent.send = capture  # pyright: ignore[reportAttributeAccessIssue]

        from wactorz.core.actor import Message, MessageType

        msg = Message(type=MessageType.TASK, sender_id="asker", payload={"do": "something"})
        await agent._invoke_handle_task(msg, None, None, lambda payload: payload)

        assert sent, "the caller was never answered"
        assert "error" in sent[0][2]


class TestStoppingStillStops:
    """The two that mean stop rather than fail are not caught."""

    async def test_a_cancellation_still_ends_the_process_loop(self, tmp_path: Path) -> None:
        """This is how SIGTERM and SIGINT reach an agent — `app.py` cancels the
        task rather than raising, so a swallowed cancellation would hang shutdown."""
        agent = make_agent(tmp_path, WORKING_PROCESS, ScriptedLLM())
        agent._compile_code()
        agent._start_program()
        await until(lambda: agent._program_tasks != [])

        task = agent._program_tasks[0]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert task.done()
        assert agent.metrics.errors == 0, "a cancellation was counted as a program error"

    async def test_a_keyboard_interrupt_is_not_swallowed(self, tmp_path: Path) -> None:
        """Swallowing it would leave the operator unable to interrupt the process."""
        agent = make_agent(tmp_path, WORKING_PROCESS, ScriptedLLM())
        agent._compile_code()

        async def interrupting(_api: Any) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            await agent._run_process_forever(interrupting)

        assert agent.metrics.errors == 0, "an interrupt was counted as a program error"


# ── The node runner ───────────────────────────────────────────────────────────


class _StubRunner:
    """Stands in for the node's runner, recording what was published."""

    node_name = "test-node"

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def publish(self, topic: str, data: Any, retain: bool = False) -> None:
        self.events.append((topic, data))


def make_remote_agent(tmp_path: Path, code: str) -> Any:
    """A node-side agent around the given program, no broker involved."""
    agent = _RemoteAgent({"name": "exiter", "code": code}, _StubRunner(), state_dir=str(tmp_path))  # pyright: ignore[reportArgumentType]
    assert agent._compile() is None
    return agent


class TestANodeIsCoveredToo:
    """A node runs the same model-written programs; an exit ends the agent
    there too, not the node. These paths await the program directly, so no
    boxing is needed — but the catch has to be there."""

    async def test_a_process_that_exits_is_counted_and_the_node_lives(self, tmp_path: Path) -> None:
        agent = make_remote_agent(tmp_path, EXITING_PROCESS)
        agent._running = True
        task = asyncio.create_task(agent._process_loop())

        alive = {"ticks": 0}

        async def bystander() -> None:
            while True:
                alive["ticks"] += 1
                await asyncio.sleep(0)

        other = asyncio.create_task(bystander())
        try:
            await until(lambda: any(topic.endswith("/errors") for topic, _ in agent._runner.events))
            before = alive["ticks"]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert alive["ticks"] > before, "the loop stopped running other work"
        finally:
            other.cancel()
            agent._running = False
            task.cancel()
            await asyncio.gather(other, task, return_exceptions=True)

    async def test_repeated_exits_escalate_as_an_error_not_an_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escalation the supervisor reads is a plain exception; a
        SystemExit reaching it would end the node instead."""
        real_sleep = asyncio.sleep

        async def instant(_seconds: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr("wactorz.remote_runner.asyncio.sleep", instant)

        agent = make_remote_agent(tmp_path, EXITING_PROCESS)
        agent._running = True
        task = asyncio.create_task(agent._process_loop())

        await asyncio.gather(task, return_exceptions=True)

        assert isinstance(task.exception(), ProcessEscalated)

    async def test_a_handle_task_that_exits_answers_the_caller(self, tmp_path: Path) -> None:
        agent = make_remote_agent(tmp_path, EXITING_HANDLE_TASK)

        result = await agent.handle_task({"do": "something"})

        assert "error" in result
