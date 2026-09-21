"""The agent a node runs is the agent main runs.

A node used to carry its own retelling of the agent contract, because the file
deployed to it could not import the package. It installs the package now, so
`NodeAgent` is a `DynamicAgent` and the surface generated code is handed is the
same `AgentAPI` — which is what these pin, along with the four things that are
genuinely different on a node: where its messages go, where its memory lives,
what answers its LLM calls, and how a task reaches it.

The shared surface itself is covered in `test_dynamic_agent_api_surface.py` and
the sliding window in `test_topic_bus_window.py`; nothing here repeats them.
"""

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from wactorz import cli
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core.actor import derive_actor_id
from wactorz.node import cli as node_cli
from wactorz.node.agent import NodeAgent
from wactorz.node.llm import BridgeProvider
from wactorz.node.runner import NodeRunner


@pytest.fixture(name="runner")
def runner_fixture(tmp_path: Path) -> NodeRunner:
    return NodeRunner("localhost", 1883, "node-a", state_dir=str(tmp_path))


def _agent(runner: NodeRunner, code: str = "", **config: Any) -> NodeAgent:
    return NodeAgent({"name": "edge-agent", "code": code, **config}, runner)


class TestItIsTheSameAgent:
    def test_a_node_agent_is_a_dynamic_agent(self, runner: NodeRunner) -> None:
        assert isinstance(_agent(runner), DynamicAgent)

    def test_generated_code_is_handed_the_shared_api(self, runner: NodeRunner) -> None:
        # Not a lookalike: the class itself, so a method added for main is on a
        # node the same day rather than when someone remembers to copy it.
        assert type(_agent(runner)._api) is AgentAPI

    @pytest.mark.parametrize(
        "method",
        [
            # Were main's alone. A program written on main reached for these on
            # a node and found nothing there.
            "run_in_background",
            "send_to_many",
            "notify_user",
            "query_ts",
            "wiring_opportunities",
            # Were a node's alone, and reached the other way.
            "ask_llm",
            "set_status",
            "publish_detection",
            "declare_contract",
        ],
    )
    def test_the_surface_is_the_union_of_what_each_side_had(
        self, runner: NodeRunner, method: str
    ) -> None:
        assert callable(getattr(_agent(runner)._api, method))

    def test_the_actor_id_is_derived_from_the_name(self, runner: NodeRunner) -> None:
        # uuid5, not uuid4: a node that restarts must present the same actor to
        # main, or every reboot looks like a new agent. Derived by the same
        # function main uses, so the two agree about which agent is which.
        assert _agent(runner).actor_id == derive_actor_id("edge-agent")

    def test_a_different_name_is_a_different_actor(self, runner: NodeRunner) -> None:
        assert _agent(runner, name="a").actor_id != _agent(runner, name="b").actor_id

    def test_an_unnamed_agent_still_gets_a_name(self, runner: NodeRunner) -> None:
        assert NodeAgent({}, runner).name.startswith("remote-agent-")


class TestWhatIsDifferentOnANode:
    def test_it_knows_which_node_it_runs_on(self, runner: NodeRunner) -> None:
        agent = _agent(runner)

        # Read by generated code as `agent.node`, and carried by every
        # heartbeat so the dashboard can place the agent without asking.
        assert agent.node == agent._api.node == "node-a"
        assert agent._build_heartbeat()["node"] == "node-a"

    def test_generated_code_is_told_the_broker_this_node_dials(self, tmp_path: Path) -> None:
        """`agent._mqtt_broker` is what a program opening its own client reads.

        The API used to copy it at construction, before the node had assigned
        it — so every agent on every node handed generated code
        `localhost:1883` while the node itself talked to the real broker.
        """
        runner = NodeRunner("broker.lan", 8883, "node-a", state_dir=str(tmp_path))
        api = _agent(runner)._api

        assert (api._mqtt_broker, api._mqtt_port) == ("broker.lan", 8883)

    def test_it_follows_the_actor_when_the_broker_is_set_after_construction(
        self, runner: NodeRunner
    ) -> None:
        # The supervisor's inject step sets these on an actor it has just
        # built, which is the other order this has to survive.
        agent = _agent(runner)
        agent._mqtt_broker = "elsewhere.lan"
        agent._mqtt_port = 1884

        assert (agent._api._mqtt_broker, agent._api._mqtt_port) == ("elsewhere.lan", 1884)

    def test_its_publishes_go_through_the_runners_queue(self, runner: NodeRunner) -> None:
        # There is no MQTTPublisher on a node; the bounded queue stands in for
        # the client, so `Actor._mqtt_publish` needs no node-specific branch.
        assert _agent(runner)._mqtt_client is runner.publisher

    def test_its_llm_calls_are_answered_by_main(self, runner: NodeRunner) -> None:
        agent = _agent(runner)

        # The key stays on main. `agent.llm` exists all the same, so the same
        # program runs in both places.
        assert isinstance(agent._llm_provider, BridgeProvider)
        assert agent._api.llm is not None

    async def test_a_bridged_call_names_a_reply_topic_under_this_node(
        self, runner: NodeRunner
    ) -> None:
        agent = _agent(runner)
        sent: list[tuple[str, Any]] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            sent.append((topic, payload))
            # Answer as main would, so the call does not wait out its timeout.
            # Only the request carries a reply topic; the metrics frame the
            # call publishes afterwards does not.
            if isinstance(payload, dict) and "_reply_topic" in payload:
                agent.deliver_reply(payload["_reply_topic"], {"text": "42"})

        agent._mqtt_publish = _publish  # type: ignore[method-assign]

        assert await agent._api.ask_llm("how many?") == "42"

        topic, payload = sent[0]
        assert topic == "main/llm_request"
        assert payload["_reply_topic"].startswith("nodes/node-a/reply/")
        assert payload["agent"] == "edge-agent"

    async def test_a_bridged_call_that_is_never_answered_gives_up(self, runner: NodeRunner) -> None:
        # `wait_for`, not `asyncio.timeout`: a node may be running Python 3.10.
        agent = _agent(runner)

        async def _publish(*_a: Any, **_kw: Any) -> None:
            return None

        agent._mqtt_publish = _publish  # type: ignore[method-assign]

        assert await agent.ask_main("main/llm_request", {}, timeout=0.05) is None
        # The key is dropped, so a reply arriving afterwards resolves nothing.
        assert not agent._result_futures

    async def test_a_reply_for_another_agent_is_not_claimed(self, runner: NodeRunner) -> None:
        assert _agent(runner).deliver_reply("nodes/node-a/reply/nobody", {"text": "x"}) is False


class TestTheNameANodeAnswersTo:
    """`/deploy` writes `WACTORZ_NODE` into every node's `.env`.

    A launcher that sets the environment and passes no name is therefore an
    ordinary case, not an exotic one — and a node that ignored it would come up
    under a random name main has never heard of and will never address.
    """

    def test_the_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_NODE", "from-env")
        args = argparse.Namespace(node="from-flag", name=None)

        assert node_cli.node_name_from(args) == "from-flag"

    def test_the_runners_old_flag_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A unit written against the single-file runner passes `--name`.
        monkeypatch.setenv("WACTORZ_NODE", "from-env")
        args = argparse.Namespace(node=None, name="from-name")

        assert node_cli.node_name_from(args) == "from-name"

    def test_the_environment_is_used_when_no_flag_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_NODE", "rpi-kitchen")
        args = argparse.Namespace(node=None, name=None)

        assert node_cli.node_name_from(args) == "rpi-kitchen"

    def test_with_nothing_at_all_it_invents_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_NODE", raising=False)
        args = argparse.Namespace(node=None, name=None)

        assert node_cli.node_name_from(args).startswith("node-")


class TestReachingAnAgentElsewhere:
    async def test_send_to_addresses_an_unknown_agent_by_name(self, runner: NodeRunner) -> None:
        """A node cannot look up which machine holds an agent — main can.

        Refusing for want of that lookup made `send_to` unusable from a node
        entirely. Every node subscribes to `agents/by-name/+/task`, so naming
        the agent reaches it wherever among them it runs.
        """
        agent = _agent(runner)
        sent: list[tuple[str, Any]] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            sent.append((topic, payload))

        agent._mqtt_publish = _publish  # type: ignore[method-assign]
        await runner.registry.register(agent)

        # What comes back is not the point and cannot be had here: the reply is
        # awaited on a connection of its own, which tests refuse. That the task
        # went out at all is the behaviour — it used to return "not found"
        # without publishing anything.
        with contextlib.suppress(ConnectionRefusedError, asyncio.TimeoutError):
            await asyncio.wait_for(
                agent._api.send_to("elsewhere", {"city": "Athens"}, timeout=0.05), timeout=2
            )

        (topic, payload) = sent[0]
        assert topic == "agents/by-name/elsewhere/task"
        assert payload["city"] == "Athens"
        assert payload["_reply_topic"].startswith("agents/by-name/edge-agent/reply/")

    async def test_an_agent_on_this_node_is_reached_in_process(self, runner: NodeRunner) -> None:
        # No broker involved when both are here: the registry delivers it.
        await runner.spawn_agent(
            {
                "name": "worker",
                "code": "async def handle_task(agent, payload):\n    return {'did': payload['do']}\n",
            }
        )
        # Spawned rather than built: the reply comes back through the caller's
        # own mailbox, so it has to be running to receive it.
        await runner.spawn_agent({"name": "caller", "code": ""})
        caller = runner.get("caller")
        assert caller is not None

        result = await asyncio.wait_for(
            caller._api.send_to("worker", {"do": "the thing"}, timeout=2), timeout=5
        )

        assert isinstance(result, dict)
        assert result["did"] == "the thing"


class TestCommandsSentStraightToTheAgent:
    """`stop` and `delete` can arrive on `agents/<id>/commands`, bypassing the
    node's control plane — its agents hold a command listener of their own.

    The base class handles only half of it here, which is why the node agent
    overrides it: the node's own bookkeeping is what makes a stop a stop and a
    delete irreversible.
    """

    async def test_a_stop_takes_the_agent_off_the_node(self, runner: NodeRunner) -> None:
        await runner.spawn_agent({"name": "edge-agent", "code": ""})
        agent = runner.get("edge-agent")
        assert agent is not None

        assert await agent.apply_command("stop") is True

        # Left registered, it stays in the node's heartbeat and refuses a
        # respawn that does not say `replace`.
        assert runner.get("edge-agent") is None

    async def test_a_delete_leaves_nothing_of_the_agent_behind(self, runner: NodeRunner) -> None:
        """Including the directory the actor base class makes for it.

        Every agent gets one for the pickle store, which a node does not use —
        its memory is a flat JSON file. Left behind, every agent ever spawned
        here leaves an empty directory on a machine chosen for being small.
        """
        await runner.spawn_agent({"name": "edge-agent", "code": ""})
        agent = runner.get("edge-agent")
        assert agent is not None
        agent.persist("count", 3)
        state_dir = agent._persistence_dir
        assert state_dir.is_dir(), "the base class did not make one after all"

        await agent.apply_command("delete")

        assert not agent._state_file.path.exists()
        assert not state_dir.exists()

    async def test_a_delete_keeps_anything_unexpected_in_that_directory(
        self, runner: NodeRunner
    ) -> None:
        # `rmdir`, not a recursive remove: something in there that this does not
        # know about survives to be looked at rather than being taken with it.
        await runner.spawn_agent({"name": "edge-agent", "code": ""})
        agent = runner.get("edge-agent")
        assert agent is not None
        stray = agent._persistence_dir / "something.db"
        stray.write_text("not ours", encoding="utf-8")

        await agent.apply_command("delete")

        assert stray.exists()

    async def test_a_delete_removes_the_memory_a_stop_keeps(self, runner: NodeRunner) -> None:
        await runner.spawn_agent({"name": "edge-agent", "code": ""})
        agent = runner.get("edge-agent")
        assert agent is not None
        agent.persist("count", 3)

        assert await agent.apply_command("delete") is True

        assert not agent._state_file.path.exists(), "the agent would come back with its memory"

    async def test_policy_still_refuses_what_it_refused(self, runner: NodeRunner) -> None:
        await runner.spawn_agent({"name": "edge-agent", "code": ""})
        agent = runner.get("edge-agent")
        assert agent is not None
        agent.essential = True

        assert await agent.apply_command("stop") is False
        assert runner.get("edge-agent") is agent


class TestTasksArriveOnATopic:
    async def test_it_runs_handle_task_and_returns_the_result(self, runner: NodeRunner) -> None:
        agent = _agent(
            runner,
            code=("async def handle_task(agent, payload):\n    return {'echo': payload['say']}\n"),
        )
        agent._compile_code(agent._code)

        assert await agent.run_task({"say": "hi"}) == {"echo": "hi"}

    async def test_an_agent_without_one_says_so_rather_than_failing(
        self, runner: NodeRunner
    ) -> None:
        agent = _agent(runner, code="async def process(agent):\n    pass\n")
        agent._compile_code(agent._code)

        result = await agent.run_task({})

        assert "no handle_task" in result["error"]

    async def test_a_failing_task_answers_the_caller_rather_than_hanging(
        self, runner: NodeRunner
    ) -> None:
        # The caller is waiting on a reply topic with a timeout. An exception
        # that escaped would leave it waiting out the whole of it for an answer
        # that was never coming.
        agent = _agent(
            runner,
            code=("async def handle_task(agent, payload):\n    raise RuntimeError('boom')\n"),
        )
        agent._compile_code(agent._code)

        result = await agent.run_task({})

        assert result["error_phase"] == "handle_task"
        assert "boom" in result["error"]

    async def test_a_task_that_never_returns_answers_anyway(self, runner: NodeRunner) -> None:
        """The caller is waiting on a reply topic with a timeout of its own.

        A handler that hangs would otherwise leave it waiting out the whole of
        it for an answer that was never coming, and leave the node holding a
        task that is going nowhere.
        """
        agent = _agent(
            runner,
            code=(
                "import asyncio\n"
                "async def handle_task(agent, payload):\n"
                "    await asyncio.sleep(3600)\n"
            ),
        )
        agent._compile_code(agent._code)
        agent._HANDLE_TASK_TIMEOUT = 0.05  # type: ignore[misc]

        result = await asyncio.wait_for(agent.run_task({}), timeout=5)

        assert result["error_phase"] == "handle_task"
        assert "timed out" in result["error"]

    async def test_an_ordinary_failure_is_reported_to_the_caller(self, runner: NodeRunner) -> None:
        agent = _agent(
            runner,
            code=("async def handle_task(agent, payload):\n    return 1 / 0\n"),
        )
        agent._compile_code(agent._code)
        sent: list[str] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            sent.append(topic)

        agent._mqtt_publish = _publish  # type: ignore[method-assign]

        result = await agent.run_task({})

        assert result["error_phase"] == "handle_task"
        assert "division by zero" in result["error"]
        # And an operator can see it, not only the caller.
        assert any(t.endswith("/errors") for t in sent)

    async def test_an_interrupt_is_not_swallowed(self, runner: NodeRunner) -> None:
        # Ctrl-C belongs to whoever is stopping the process, not to the task.
        agent = _agent(
            runner,
            code=("async def handle_task(agent, payload):\n    raise KeyboardInterrupt\n"),
        )
        agent._compile_code(agent._code)

        with pytest.raises(KeyboardInterrupt):
            await agent.run_task({})

    async def test_a_program_that_exits_takes_the_task_down_and_not_the_node(
        self, runner: NodeRunner
    ) -> None:
        # A node runs the same model-written programs main does, and `sys.exit`
        # inside one must not end the process every other agent shares.
        agent = _agent(
            runner,
            code=("async def handle_task(agent, payload):\n    raise SystemExit(1)\n"),
        )
        agent._compile_code(agent._code)

        result = await agent.run_task({})

        assert result["error_phase"] == "handle_task"


class TestCompile:
    def test_it_binds_the_lifecycle_functions(self, runner: NodeRunner) -> None:
        agent = _agent(
            runner,
            code=(
                "async def setup(agent):\n    pass\n"
                "async def process(agent):\n    pass\n"
                "async def handle_task(agent, payload):\n    return {}\n"
            ),
        )

        assert agent._compile_code(agent._code) is None
        assert agent._fn_setup and agent._fn_process and agent._fn_handle_task

    def test_a_syntax_error_is_returned_not_raised(self, runner: NodeRunner) -> None:
        # The runner publishes it as a fatal error event; raising here would
        # take down the spawn instead of reporting it.
        agent = _agent(runner, code="def broken(:\n")

        error = agent._compile_code(agent._code)

        assert error is not None
        assert "edge-agent" in error or "SyntaxError" in error


class TestSubscribeStillTeaches:
    """The caller is model-written code, so the errors have to say what to write.

    These run through a node agent because that is where the surface used to
    differ; the behaviour itself now belongs to the shared API.
    """

    async def test_a_non_callable_is_refused(self, runner: NodeRunner) -> None:
        with pytest.raises(TypeError) as caught:
            _agent(runner)._api.subscribe("sensors/x", "not a function")

        assert "requires a callable callback" in str(caught.value)
        assert "sensors/x" in str(caught.value)

    async def test_none_is_refused(self, runner: NodeRunner) -> None:
        with pytest.raises(TypeError):
            _agent(runner)._api.subscribe("sensors/x", None)

    async def test_a_callback_taking_no_payload_is_refused(self, runner: NodeRunner) -> None:
        async def takes_nothing() -> None:
            return None

        with pytest.raises(TypeError) as caught:
            _agent(runner)._api.subscribe("sensors/x", takes_nothing)

        assert "one argument" in str(caught.value)


class TestTheManifestReachesThePlanner:
    async def test_it_carries_what_the_spawn_declared(self, runner: NodeRunner) -> None:
        agent = _agent(
            runner,
            capabilities=["temperature"],
            publishes=["sensors/temp"],
            subscribes=["commands/#"],
            description="reads a probe",
        )
        sent: list[tuple[str, Any]] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            sent.append((topic, payload))

        agent._mqtt_publish = _publish  # type: ignore[method-assign]

        await agent._api._publish_manifest()

        (topic, manifest) = sent[0]
        assert topic == f"agents/{agent.actor_id}/manifest"
        assert manifest["node"] == "node-a"
        assert manifest["capabilities"] == ["temperature"]
        assert manifest["publishes"] == ["sensors/temp"]
        assert manifest["subscribes"] == ["commands/#"]
        assert manifest["description"] == "reads a probe"

    async def test_declaring_a_contract_adds_to_the_spawn_rather_than_replacing_it(
        self, runner: NodeRunner
    ) -> None:
        # An agent spawned with `publishes` whose setup() then declares its own
        # used to lose the first, and with it the planner's only description of
        # the agent before its first publish.
        agent = _agent(runner, publishes=["sensors/temp"])
        sent: list[Any] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            sent.append(payload)

        agent._mqtt_publish = _publish  # type: ignore[method-assign]

        agent._api.declare_contract(publishes=["sensors/humidity"])
        await asyncio.sleep(0)
        await agent._api._publish_manifest()

        assert sent[-1]["publishes"] == ["sensors/humidity", "sensors/temp"]


class TestWhichRoleTheProcessTakes:
    """`--node` chooses the role; `WACTORZ_NODE` only supplies the name.

    A bare environment variable must not turn a server into a node: `/deploy`
    writes `WACTORZ_NODE` into a node's `.env`, but a variable can reach a
    server's environment too, and the failure there is a Wactorz install that
    silently stops being one.
    """

    @staticmethod
    def _args(argv: list[str]) -> argparse.Namespace:
        with mock.patch.object(sys, "argv", ["wactorz", *argv]):
            return cli.get_args()

    def test_a_named_flag_is_node_mode(self) -> None:
        assert self._args(["--node", "rpi"]).node == "rpi"

    def test_a_bare_flag_is_node_mode_too(self) -> None:
        # Node mode with the name left to the environment — an empty string,
        # which is not None, is what says so.
        assert self._args(["--node"]).node == ""

    def test_no_flag_is_the_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_NODE", "rpi-kitchen")

        assert self._args([]).node is None

    def test_a_bare_flag_takes_the_name_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_NODE", "rpi-kitchen")

        assert node_cli.node_name_from(self._args(["--node"])) == "rpi-kitchen"
