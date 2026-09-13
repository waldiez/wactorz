"""A generated agent has a way to say it is finished.

Asked for an agent that stops after a while, a model reaches for `sys.exit()`,
because until now the API offered nothing else. That no longer takes the process
down, but it is still counted as a crash: repaired by the model at three
consecutive errors, and retired by the Supervisor at five — so an agent that did
exactly what it was asked gets billed repairs and a notification asking someone
to intervene. Worse, a successful repair removes the exit, leaving a "stop after
45 seconds" agent running for ever doing nothing.

`agent.stop()` is that missing ending. It is a removal, not a pause: nothing the
agent could do afterwards would restart it, and an agent that has finished what
it was spawned for should not return on the next restart.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.main.actor import MainActor
from wactorz.core.actor import ActorState, Message, MessageType

STOPPING_PROCESS = """
async def process(agent):
    await agent.stop()
    return
"""

STOPPING_HANDLE_TASK = """
async def process(agent):
    pass

async def handle_task(agent, payload):
    await agent.stop()
    return {'result': 'all done'}
"""

TRAILING_WORK = """
async def process(agent):
    await agent.stop()
    agent.state['ran_after_stop'] = True
"""


class Registry:
    def __init__(self) -> None:
        self.unregistered: list[str] = []
        self._supervisor_ref = Supervisor()
        self.delivered: list[tuple[str, Any]] = []
        self._by_name: dict[str, Any] = {}

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)

    def find_by_name(self, name: str) -> Any:
        return self._by_name.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._by_name.values())

    async def deliver(self, target_id: str, msg: Message) -> bool:
        self.delivered.append((target_id, msg.payload))
        return True


class Supervisor:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, name: str) -> None:
        self.released.append(name)


def make_main(dropped: list[str]) -> Any:
    """A real MainActor, because `find_main_actor` checks the type on purpose —
    anything registered under the name would otherwise satisfy it, and the
    AttributeError would surface far from the cause."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    setattr(main, "_remove_from_spawn_registry", dropped.append)
    return main


class Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain))

    async def disconnect(self) -> None:
        return None

    def withdrawn(self) -> list[str]:
        return [t for t, payload, retain in self.published if retain and payload == b""]


def make_agent(tmp_path: Path, code: str) -> tuple[DynamicAgent, Registry, list[str], Broker]:
    agent = DynamicAgent(name="finisher", code=code, poll_interval=0, persistence_dir=str(tmp_path))
    agent.state = ActorState.RUNNING
    registry, broker = Registry(), Broker()
    dropped: list[str] = []
    registry._by_name["main"] = make_main(dropped)
    agent._registry = registry  # pyright: ignore[reportAttributeAccessIssue]
    agent._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]
    agent._compile_code()
    return agent, registry, dropped, broker


class TestEndingIsARemoval:
    async def test_it_leaves_supervision(self, tmp_path: Path) -> None:
        """Otherwise the heartbeat watchdog reads the stop as a crash and
        restarts the agent that just finished."""
        agent, registry, _dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        assert registry._supervisor_ref.released == ["finisher"]

    async def test_it_leaves_the_spawn_registry(self, tmp_path: Path) -> None:
        """So a restart does not restore an agent that has already finished."""
        agent, _registry, dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        assert dropped == ["finisher"]

    async def test_it_leaves_the_actor_registry(self, tmp_path: Path) -> None:
        agent, registry, _dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        assert registry.unregistered == [agent.actor_id]

    async def test_it_withdraws_its_manifest(self, tmp_path: Path) -> None:
        """The signal main and the dashboard both act on — and the only one that
        reaches main at all when the agent runs on a node."""
        agent, _registry, _dropped, broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        assert f"agents/{agent.actor_id}/manifest" in broker.withdrawn()

    async def test_the_withdrawal_comes_after_the_final_status(self, tmp_path: Path) -> None:
        """A status published after it would read as an agent that still exists."""
        agent, _registry, _dropped, broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        topics = [t for t, _p, _r in broker.published]
        withdrawal = len(topics) - 1 - topics[::-1].index(f"agents/{agent.actor_id}/manifest")
        last_status = len(topics) - 1 - topics[::-1].index(f"agents/{agent.actor_id}/status")
        assert withdrawal > last_status

    async def test_the_actor_is_stopped(self, tmp_path: Path) -> None:
        agent, _registry, _dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()

        assert agent.state == ActorState.STOPPED


class TestCalledFromInsideTheProgram:
    async def test_a_process_that_stops_itself_ends_its_own_loop(self, tmp_path: Path) -> None:
        """The process loop is the task calling this, so the wind-down has to
        leave it alone and let it exit on the state it just set."""
        agent, _registry, dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)
        agent._start_program()

        for _ in range(200):
            await asyncio.sleep(0)
            if agent.state == ActorState.STOPPED:
                break

        assert agent.state == ActorState.STOPPED
        assert dropped == ["finisher"]
        await asyncio.gather(*agent._program_tasks, return_exceptions=True)

    async def test_code_after_stop_still_runs(self, tmp_path: Path) -> None:
        """stop() is not exit. The prompt teaches `await agent.stop()` then
        `return` for exactly this reason, and the behaviour is pinned so the
        guidance stays true."""
        agent, _registry, _dropped, _broker = make_agent(tmp_path, TRAILING_WORK)

        await agent._fn_process(agent._api)  # pyright: ignore[reportOptionalCall]

        assert agent._api.state.get("ran_after_stop") is True

    async def test_a_handle_task_reply_still_reaches_the_caller(self, tmp_path: Path) -> None:
        """The reply is sent after the function returns. Stopping first must not
        swallow it, or the caller waits out its whole timeout for nothing."""
        agent, registry, _dropped, _broker = make_agent(tmp_path, STOPPING_HANDLE_TASK)
        msg = Message(type=MessageType.TASK, sender_id="asker", payload={})

        await agent._invoke_handle_task(msg, None, None, lambda payload: payload)

        assert registry.delivered == [("asker", {"result": "all done"})]


class TestItIsSafeToRepeat:
    async def test_ending_twice_does_the_work_once(self, tmp_path: Path) -> None:
        """A process loop finishing its tick can reach it again."""
        agent, registry, dropped, _broker = make_agent(tmp_path, STOPPING_PROCESS)

        await agent.end_self()
        await agent.end_self()

        assert dropped == ["finisher"]
        assert registry.unregistered == [agent.actor_id]
        assert registry._supervisor_ref.released == ["finisher"]

    async def test_it_survives_having_no_registry(self, tmp_path: Path) -> None:
        """An agent constructed without one still has to be able to finish."""
        agent = DynamicAgent(
            name="lonely", code=STOPPING_PROCESS, poll_interval=0, persistence_dir=str(tmp_path)
        )
        agent.state = ActorState.RUNNING
        agent._mqtt_client = Broker()  # pyright: ignore[reportAttributeAccessIssue]

        await agent.end_self()

        assert agent.state == ActorState.STOPPED


class TestThePromptTeachesIt:
    def test_the_api_listing_names_it(self) -> None:
        """An API method the prompt does not teach will not be used, and the
        model will keep reaching for sys.exit() instead."""
        from wactorz.agents.prompts import main_actor_prompts

        source = main_actor_prompts.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        assert "agent.stop()" in text

    def test_it_is_reachable_on_the_api_object(self) -> None:
        from wactorz.agents.dynamic.api import AgentAPI

        assert callable(AgentAPI.stop)


@pytest.mark.parametrize("code", [STOPPING_PROCESS, TRAILING_WORK])
async def test_any_program_shape_can_end(tmp_path: Path, code: str) -> None:
    agent, _registry, dropped, _broker = make_agent(tmp_path, code)

    await agent._fn_process(agent._api)  # pyright: ignore[reportOptionalCall]

    assert dropped == ["finisher"]


class TestTheNodeSideOffersTheSameVerb:
    """The prompt teaches `agent.stop()` to every generated program, and a
    program does not know at authoring time where it will be deployed. A verb
    that exists only on the host would give a node agent an AttributeError —
    counted as a process() error, repaired, and retired: the very cycle this
    feature exists to end, triggered by the prompt that teaches it.
    """

    def test_a_node_agent_has_stop_too(self) -> None:
        from wactorz.remote_runner import _RemoteAgentAPI

        assert callable(_RemoteAgentAPI.stop)

    def test_every_verb_the_prompt_teaches_exists_on_both(self) -> None:
        """The two APIs are separate classes that have to stay in step."""
        from wactorz.agents.dynamic.api import AgentAPI
        from wactorz.remote_runner import _RemoteAgentAPI

        for verb in ("stop", "publish", "subscribe", "log", "persist", "recall"):
            assert hasattr(AgentAPI, verb), f"host API is missing {verb}"
            assert hasattr(_RemoteAgentAPI, verb), f"node API is missing {verb}"

    async def test_it_stops_and_withdraws(self, tmp_path: Path) -> None:
        """All this side can do, and all it needs to: main reacts to the
        withdrawal for the spawn registry and the node's desired state."""
        from wactorz.remote_runner import _RemoteAgent, _RemoteAgentAPI

        published: list[tuple[str, Any, bool]] = []

        class Runner:
            def __init__(self) -> None:
                self._agents: dict[str, Any] = {}

            async def publish(
                self, topic: str, payload: Any, retain: bool = False, qos: int = 0
            ) -> None:
                published.append((topic, payload, retain))

        agent = _RemoteAgent.__new__(_RemoteAgent)
        agent.name = "finisher"
        agent._ending = False
        agent._runner = Runner()  # pyright: ignore[reportAttributeAccessIssue]
        agent._runner._agents["finisher"] = agent
        stopped: list[bool] = []

        async def _stop() -> None:
            stopped.append(True)

        agent.stop = _stop  # pyright: ignore[reportAttributeAccessIssue]
        agent.actor_id = "abc123"  # pyright: ignore[reportAttributeAccessIssue]
        api = _RemoteAgentAPI.__new__(_RemoteAgentAPI)
        api._agent = agent

        await api.stop()

        assert stopped == [True]
        assert ("agents/abc123/manifest", b"", True) in published
        assert "finisher" not in agent._runner._agents

    async def test_ending_twice_does_the_work_once(self, tmp_path: Path) -> None:
        from wactorz.remote_runner import _RemoteAgent, _RemoteAgentAPI

        published: list[tuple[str, Any, bool]] = []

        class Runner:
            def __init__(self) -> None:
                self._agents: dict[str, Any] = {}

            async def publish(
                self, topic: str, payload: Any, retain: bool = False, qos: int = 0
            ) -> None:
                published.append((topic, payload, retain))

        agent = _RemoteAgent.__new__(_RemoteAgent)
        agent.name = "finisher"
        agent._ending = False
        agent._runner = Runner()  # pyright: ignore[reportAttributeAccessIssue]

        async def _stop() -> None:
            return None

        agent.stop = _stop  # pyright: ignore[reportAttributeAccessIssue]
        agent.actor_id = "abc123"  # pyright: ignore[reportAttributeAccessIssue]
        api = _RemoteAgentAPI.__new__(_RemoteAgentAPI)
        api._agent = agent

        await api.stop()
        await api.stop()

        assert len(published) == 1

    async def test_the_withdrawal_survives_stopping_the_task_that_asked(
        self, tmp_path: Path
    ) -> None:
        """The case a rewrite would silently break, and the common one.

        `_RemoteAgent.stop()` cancels every task the agent owns and does not
        spare the one calling it — unlike the host, whose wind-down excludes the
        current task. So a program ending itself is cancelled part-way through
        this method, and only a `finally` gets the withdrawal out. Without it
        the agent stops on the node and the host never learns it is gone: no
        spawn-registry drop, no desired-state rewrite, and it returns on the
        next reconcile.
        """
        from wactorz.remote_runner import _RemoteAgent, _RemoteAgentAPI

        published: list[tuple[str, Any, bool]] = []

        class Runner:
            def __init__(self) -> None:
                self._agents: dict[str, Any] = {}

            async def publish(
                self, topic: str, payload: Any, retain: bool = False, qos: int = 0
            ) -> None:
                published.append((topic, payload, retain))

        agent = _RemoteAgent.__new__(_RemoteAgent)
        agent.name = "finisher"
        agent.actor_id = "abc123"  # pyright: ignore[reportAttributeAccessIssue]
        agent._ending = False
        agent._runner = Runner()  # pyright: ignore[reportAttributeAccessIssue]
        agent._tasks = []

        async def _stop() -> None:
            # What the real one does: cancel every task, the caller included.
            for task in agent._tasks:
                task.cancel()
            agent._tasks.clear()
            await asyncio.sleep(0)  # let the cancellation land

        agent.stop = _stop  # pyright: ignore[reportAttributeAccessIssue]
        api = _RemoteAgentAPI.__new__(_RemoteAgentAPI)
        api._agent = agent

        async def program() -> None:
            await api.stop()

        task = asyncio.ensure_future(program())
        agent._tasks.append(task)
        await asyncio.gather(task, return_exceptions=True)

        assert task.cancelled(), "the caller was not cancelled — the test proves nothing"
        assert ("agents/abc123/manifest", b"", True) in published
