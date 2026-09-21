"""What a node does with the commands main sends it.

Everything a node does is in answer to something on `nodes/<name>/...`. These
drive those handlers directly, with the publisher's queue standing in for the
broker, so what is checked is the decision the node makes rather than the
plumbing under it.
"""

import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import ActorState
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner

CODE = "async def process(agent):\n    pass\n"
REPAIRED = "async def process(agent):\n    pass  # repaired by the LLM\n"


class RecordingRunner(NodeRunner):
    """A node whose messages are recorded rather than queued for a broker.

    Its agents publish through `runner.publisher`, and the runner itself
    through `runner.publish`, so both are pointed here — what a test reads is
    everything this node would have sent.
    """

    def __init__(self, tmp_path: Path) -> None:
        super().__init__("localhost", 1883, "rpi", state_dir=str(tmp_path))
        self.published: list[tuple[str, Any, bool]] = []
        self.publisher.publish = self.publish  # type: ignore[method-assign]

    async def publish(self, topic: str, data: Any, retain: bool = False, **_kw: Any) -> None:
        self.published.append((topic, data, retain))

    @property
    def topics(self) -> list[str]:
        return [topic for topic, _payload, _retain in self.published]


@pytest.fixture(name="eager_runner")
async def eager_runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    """A node whose supervisor polls fast enough to observe within a test.

    Set before `start`, not after: the watch loop reads the interval when it
    goes to sleep, so a change made afterwards takes effect only once the
    default two seconds have already passed — long after a test has finished,
    which makes anything it was meant to observe invisible.
    """
    runner = RecordingRunner(tmp_path)
    runner.supervisor._poll_interval = 0.01
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.supervisor.stop()


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    """A running node, with whatever it started stopped again afterwards."""
    runner = RecordingRunner(tmp_path)
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


class TestSpawning:
    async def test_it_starts_the_agent_and_says_so(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})

        agent = runner.get("collector")
        assert isinstance(agent, NodeAgent)
        assert agent.node == "rpi"
        assert f"agents/{runner.node_name}/logs" in runner.topics

    async def test_a_second_spawn_of_the_same_name_is_declined(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        first = runner.get("collector")

        await runner.spawn_agent({"name": "collector", "code": CODE})

        assert runner.get("collector") is first

    async def test_replace_starts_a_fresh_one(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        first = runner.get("collector")

        await runner.spawn_agent({"name": "collector", "code": CODE, "replace": True})

        assert runner.get("collector") is not first

    async def test_a_migration_is_acknowledged_by_name_and_token(
        self, runner: RecordingRunner
    ) -> None:
        # The ack says the process started and nothing more: a heartbeat would
        # conflate arrival with health and roll back a migration that worked.
        await runner.spawn_agent({"name": "collector", "code": CODE, "_migration_token": "tok-1"})

        (payload,) = [p for t, p, _ in runner.published if t.endswith("/spawn_ack")]
        assert payload["agent"] == "collector"
        assert payload["migration_token"] == "tok-1"

    async def test_a_config_that_is_not_a_dict_is_ignored(self, runner: RecordingRunner) -> None:
        # It arrives off the broker, so it is whatever was published.
        await runner.spawn_agent("collector")

        assert not runner.agents


class TestSupervision:
    """A node gets the same OTP supervision main does, from the same supervisor.

    It used to run a second implementation of it. The claim these make is that
    the shared one is really in charge here — a crashed agent comes back, and a
    deliberate stop does not.
    """

    async def test_a_crashed_agent_is_restarted(self, eager_runner: RecordingRunner) -> None:
        runner = eager_runner
        await runner.spawn_agent({"name": "collector", "code": CODE, "restart_delay": 0})
        first = runner.get("collector")
        assert first is not None

        first.state = ActorState.FAILED

        for _ in range(200):
            await asyncio.sleep(0.01)
            if runner.get("collector") is not first:
                break
        fresh = runner.get("collector")
        assert fresh is not None and fresh is not first
        assert fresh.state is ActorState.RUNNING

    async def test_an_agent_that_is_slow_to_start_is_not_spawned_twice(
        self, eager_runner: RecordingRunner
    ) -> None:
        """A spec with no actor reads as "should be running and is not".

        Starting is not instantaneous — a generated program's `on_start`
        compiles it, and can ask an LLM to repair it, which outlasts the poll
        interval easily. The watch loop must not spawn a second actor alongside
        the one still starting, nor spend restart budget doing it.
        """
        runner = eager_runner
        built: list[NodeAgent] = []
        real_start = NodeAgent.start

        async def slow_start(self: NodeAgent) -> None:
            built.append(self)
            await asyncio.sleep(0.15)  # many poll intervals
            await real_start(self)

        NodeAgent.start = slow_start  # type: ignore[method-assign]
        try:
            await runner.spawn_agent({"name": "collector", "code": CODE, "restart_delay": 0})
        finally:
            NodeAgent.start = real_start  # type: ignore[method-assign]

        assert len(built) == 1, f"the watch loop spawned {len(built)} actors for one agent"
        assert runner.supervisor._specs["collector"]._restart_times == []

    async def test_a_spawn_that_fails_leaves_the_name_free(self, runner: RecordingRunner) -> None:
        # Not left retired-but-present: the next spawn of that name must be
        # able to start rather than finding a spec nothing will act on.
        real_start = NodeAgent.start

        async def failing_start(self: NodeAgent) -> None:
            raise RuntimeError("the camera is not there")

        NodeAgent.start = failing_start  # type: ignore[method-assign]
        try:
            await runner.spawn_agent({"name": "collector", "code": CODE})
        finally:
            NodeAgent.start = real_start  # type: ignore[method-assign]

        assert "collector" not in runner.supervisor._specs
        assert "collector" not in runner._configs
        assert any("Failed to start" in str(p) for _t, p, _r in runner.published)

        await runner.spawn_agent({"name": "collector", "code": CODE})
        assert runner.get("collector") is not None

    async def test_a_repaired_program_is_what_a_restart_brings_back(
        self, runner: RecordingRunner
    ) -> None:
        """An agent here can repair its own code; the repair has to survive.

        `_persist_fixed_code` patches the supervisor's factory so the next
        restart spawns the fixed program — but it reaches the supervisor through
        `registry._supervisor_ref`, which is wired by `ActorSystem` on main and
        had nothing wiring it here. Without it a node repairs the same crash on
        every restart, for ever, paying an LLM each time.
        """
        await runner.spawn_agent({"name": "collector", "code": CODE, "restart_delay": 0})
        agent = runner.get("collector")
        assert agent is not None
        repaired = "async def process(agent):\n    pass  # repaired\n"

        agent._persist_fixed_code(repaired)

        # Built the way the supervisor builds it: the patched factory is a
        # coroutine function where the original was not.
        made = runner.supervisor._specs["collector"].factory()
        rebuilt = await made if inspect.isawaitable(made) else made

        assert isinstance(rebuilt, NodeAgent)
        assert rebuilt._code == repaired

    async def test_the_latest_repair_is_the_one_that_comes_back(
        self, runner: RecordingRunner
    ) -> None:
        # An agent can be repaired more than once, and each patched factory
        # wraps the one before it. The chain has to end at the newest code
        # rather than the first repair, or at itself.
        await runner.spawn_agent({"name": "collector", "code": CODE, "restart_delay": 0})
        agent = runner.get("collector")
        assert agent is not None

        agent._persist_fixed_code("async def process(agent):\n    pass  # first\n")
        agent._persist_fixed_code("async def process(agent):\n    pass  # second\n")

        made = runner.supervisor._specs["collector"].factory()
        rebuilt = await made if inspect.isawaitable(made) else made

        assert isinstance(rebuilt, NodeAgent)
        assert "second" in rebuilt._code

    async def test_a_restart_of_the_agent_keeps_the_repair(self, runner: RecordingRunner) -> None:
        # `/nodes restart-agent` rebuilds from the config the node stored, which
        # held the program that failed — so restarting a repaired agent handed
        # back the break and paid to fix it again.
        await runner.spawn_agent({"name": "collector", "code": "# broken"})
        agent = runner.get("collector")
        assert agent is not None
        agent._persist_fixed_code(REPAIRED)

        await runner._restart_agent("collector")

        fresh = runner.get("collector")
        assert fresh is not None and fresh._code == REPAIRED

    async def test_the_supervisor_is_reachable_from_an_agent(self, runner: RecordingRunner) -> None:
        # The same back-reference is how a deliberate stop releases supervision.
        # Absent, that is a silent no-op rather than an error.
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None

        assert agent._registry is not None
        assert agent._registry._supervisor_ref is runner.supervisor

    async def test_a_deliberate_stop_is_not_restarted(self, eager_runner: RecordingRunner) -> None:
        # The watchdog must be able to tell a stop from a crash, or `/nodes`
        # stopping an agent would bring it straight back.
        runner = eager_runner
        await runner.spawn_agent({"name": "collector", "code": CODE, "restart_delay": 0})

        await runner.stop_agent("collector")
        for _ in range(20):
            await asyncio.sleep(0.01)

        assert runner.get("collector") is None


class TestTellingMainAboutARepair:
    """The node says a program changed, and hands it over only when asked.

    Code a node volunteered would be filed by main and run wherever the agent
    goes next, so the notice carries none and the program travels only in
    answer to a request quoting main's own token.
    """

    async def test_a_repair_is_announced_without_the_program(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "collector", "code": "# broken"})
        agent = runner.get("collector")
        assert agent is not None

        agent._persist_fixed_code(REPAIRED)
        await _settle()

        (notice,) = [p for t, p, _ in runner.published if t.endswith("/code_changed")]
        assert notice["agent"] == "collector"
        assert "code" not in notice, "the notice carried the program"

    async def test_the_same_program_twice_announces_once(self, runner: RecordingRunner) -> None:
        # A repair that changed nothing is not news, and every notice costs
        # main a question.
        await runner.spawn_agent({"name": "collector", "code": "# broken"})
        agent = runner.get("collector")
        assert agent is not None

        agent._persist_fixed_code(REPAIRED)
        agent._persist_fixed_code(REPAIRED)
        await _settle()

        assert len([t for t in runner.topics if t.endswith("/code_changed")]) == 1

    async def test_it_answers_a_request_with_what_it_is_running(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": "# broken"})
        agent = runner.get("collector")
        assert agent is not None
        agent._code = REPAIRED

        await runner._on_code_request(
            "nodes/rpi/code_request",
            {"agent": "collector", "token": "tok-1"},
            _Message(payload=b"{}"),
        )

        (answer,) = [p for t, p, _ in runner.published if t.endswith("/code_return")]
        assert answer["code"] == REPAIRED
        assert answer["token"] == "tok-1", "the token has to come back or main ignores it"
        assert answer["agent"] == "collector"

    async def test_a_request_for_an_agent_that_is_not_here_is_not_answered(
        self, runner: RecordingRunner
    ) -> None:
        await runner._on_code_request(
            "nodes/rpi/code_request",
            {"agent": "elsewhere", "token": "tok-1"},
            _Message(payload=b"{}"),
        )

        assert not [t for t in runner.topics if t.endswith("/code_return")]


class TestStopping:
    async def test_a_plain_stop_leaves_the_state_file(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("count", 3)

        await runner.stop_agent("collector")

        assert runner.get("collector") is None
        assert agent._state_file.path.exists(), "a stop is not a delete"

    async def test_a_delete_removes_the_state_and_clears_what_was_retained(
        self, runner: RecordingRunner
    ) -> None:
        # Without the purge the broker re-delivers the agent's last heartbeat
        # and manifest to every subscriber that connects afterwards, which is
        # what made a deleted agent come back on the next restart.
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("count", 3)
        path = agent._state_file.path

        await runner.stop_agent("collector", delete=True)

        assert not path.exists()
        cleared = {t for t, p, retain in runner.published if p == b"" and retain}
        assert f"agents/{agent.actor_id}/manifest" in cleared
        assert f"agents/{agent.actor_id}/heartbeat" in cleared

    async def test_stopping_something_that_is_not_here_is_harmless(
        self, runner: RecordingRunner
    ) -> None:
        await runner.stop_agent("never-existed")


class TestReconciling:
    async def test_a_desired_state_starts_what_is_missing(self, runner: RecordingRunner) -> None:
        # This is what brings a node's agents back after it reboots.
        message = _Message(payload=b"{}")

        await runner._on_desired_state(
            "nodes/rpi/desired_state",
            {"agents": [{"name": "collector", "code": CODE}]},
            message,
        )
        await _settle()

        assert runner.get("collector") is not None

    async def test_it_leaves_an_agent_that_is_already_running(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        first = runner.get("collector")

        await runner._on_desired_state(
            "nodes/rpi/desired_state",
            {"agents": [{"name": "collector", "code": CODE}]},
            _Message(payload=b"{}"),
        )
        await _settle()

        assert runner.get("collector") is first

    async def test_an_empty_payload_is_a_retained_message_being_cleared(
        self, runner: RecordingRunner
    ) -> None:
        await runner._on_desired_state("nodes/rpi/desired_state", None, _Message(payload=b""))

        assert not runner.agents

    async def test_restarting_an_agent_keeps_its_config_and_its_memory(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE, "poll_interval": 7})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("count", 3)

        await runner._restart_agent("collector")

        fresh = runner.get("collector")
        assert fresh is not None and fresh is not agent
        assert fresh.poll_interval == 7
        assert fresh.recall("count") == 3

    async def test_restarting_one_that_is_not_here_says_so(self, runner: RecordingRunner) -> None:
        await runner._restart_agent("never-existed")

        (payload,) = [p for t, p, _ in runner.published if t.endswith("/logs")]
        assert payload["type"] == "error"
        assert "never-existed" in payload["message"]


class TestTasks:
    async def test_a_task_reaches_the_agent_and_the_reply_goes_where_asked(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent(
            {
                "name": "collector",
                "code": "async def handle_task(agent, payload):\n    return {'got': payload}\n",
            }
        )

        await runner._on_task(
            "agents/by-name/collector/task",
            {"payload": 42, "_reply_topic": "nodes/main/reply/abc", "_remote_task": True},
            _Message(payload=b"{}"),
        )
        await _settle()

        (reply,) = [p for t, p, _ in runner.published if t == "nodes/main/reply/abc"]
        assert reply == {"got": 42}

    async def test_transport_metadata_does_not_reach_the_agent(
        self, runner: RecordingRunner
    ) -> None:
        # The agent sees the envelope, as one on main does — but not the fields
        # that only exist to route the message.
        await runner.spawn_agent(
            {
                "name": "collector",
                "code": "async def handle_task(agent, payload):\n    return {'keys': sorted(payload)}\n",
            }
        )

        await runner._on_task(
            "agents/by-name/collector/task",
            {"city": "Athens", "_reply_topic": "nodes/main/reply/abc", "_remote_task": True},
            _Message(payload=b"{}"),
        )
        await _settle()

        (reply,) = [p for t, p, _ in runner.published if t == "nodes/main/reply/abc"]
        assert reply == {"keys": ["city"]}

    async def test_a_task_for_an_agent_that_is_not_here_is_ignored(
        self, runner: RecordingRunner
    ) -> None:
        await runner._on_task(
            "agents/by-name/elsewhere/task", {"payload": 1}, _Message(payload=b"{}")
        )
        await _settle()

        assert not [t for t in runner.topics if "reply" in t]

    async def test_a_reply_nobody_is_waiting_for_is_reported(
        self, runner: RecordingRunner, caplog: pytest.LogCaptureFixture
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})

        with caplog.at_level("WARNING"):
            await runner._on_reply("nodes/rpi/reply/stale", {"text": "x"}, _Message(b"{}"))

        assert "no agent had a matching pending future" in caplog.text


class TestMigration:
    async def test_returning_to_main_ships_the_state_and_stops_the_agent(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE, "poll_interval": 7})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("count", 3)

        await runner._migrate_agent(
            {"name": "collector", "target_node": "@main", "return_token": "tok-9"}
        )

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert returned["state"] == {"count": 3}
        assert returned["return_token"] == "tok-9"
        assert returned["config"]["node"] == "rpi"
        assert runner.get("collector") is None
        # The file is kept: main deletes it once the agent is confirmed running
        # elsewhere, so a migration that fails after this has something to
        # roll back to.
        assert agent._state_file.path.exists()

    async def test_a_repaired_agent_takes_its_repair_back_to_main(
        self, runner: RecordingRunner
    ) -> None:
        """Migration hands main the program the agent is actually running.

        It sent the spawn config instead, which still held the code that
        failed — so an agent that had repaired itself on a node arrived back on
        main broken, and main repaired it over again.
        """
        await runner.spawn_agent({"name": "collector", "code": "# broken"})
        agent = runner.get("collector")
        assert agent is not None
        agent._persist_fixed_code(REPAIRED)

        await runner._migrate_agent({"name": "collector", "target_node": "@main"})

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert returned["config"]["code"] == REPAIRED

    async def test_state_that_cannot_travel_is_named_rather_than_dropped_silently(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent._persistent_state = {"count": 3, "capture": object()}

        await runner._migrate_agent({"name": "collector", "target_node": "@main"})

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert returned["state"] == {"count": 3}
        assert returned["state_keys_dropped"] == ["capture"]

    async def test_node_to_node_migration_is_refused(self, runner: RecordingRunner) -> None:
        # It was a lateral path: generated code on one node spawning code on
        # another. Main routes migrations, being the only party that can sign
        # for every node.
        await runner.spawn_agent({"name": "collector", "code": CODE})

        await runner._migrate_agent({"name": "collector", "target_node": "rpi-bedroom"})

        (result,) = [p for t, p, _ in runner.published if t.endswith("/migrate_result")]
        assert result["success"] is False
        assert "routed through main" in result["error"]
        assert runner.get("collector") is not None, "the agent was stopped anyway"

    async def test_migrating_one_that_is_not_here_reports_the_failure(
        self, runner: RecordingRunner
    ) -> None:
        await runner._migrate_agent({"name": "elsewhere", "target_node": "@main"})

        (result,) = [p for t, p, _ in runner.published if t.endswith("/migrate_result")]
        assert result["success"] is False
        assert "not found" in result["error"]


class TestShuttingDown:
    async def test_it_says_the_node_is_going_and_lets_the_process_end(self, tmp_path: Path) -> None:
        """A `stop_all` off the broker, and SIGTERM, both come through here.

        `sys.exit` in this task would not end the process — the SystemExit would
        be stored on the task and the loops would carry on. The loops are
        cancelled instead, so `run` returns and the process exits 0, which is
        what stops a unit with `Restart=on-failure` bringing the node back.
        """
        runner = RecordingRunner(tmp_path)
        running = asyncio.create_task(runner.run())
        for _ in range(8):
            await asyncio.sleep(0)

        await runner.shutdown()
        await asyncio.wait_for(running, timeout=2)

        (offline,) = [p for t, p, _ in runner.published if p.get("status") == "offline"]
        assert offline["node"] == "rpi"
        assert running.done() and not running.cancelled()

    async def test_it_stops_the_agents_it_was_running(self, tmp_path: Path) -> None:
        runner = RecordingRunner(tmp_path)
        running = asyncio.create_task(runner.run())
        for _ in range(8):
            await asyncio.sleep(0)
        await runner.spawn_agent({"name": "collector", "code": CODE})

        await runner.shutdown()
        await asyncio.wait_for(running, timeout=2)

        assert not runner.agents
        # The watch loop is stopped and waited for, so nothing it had in flight
        # registers an agent into a node that is shutting down.
        assert runner.supervisor._watch_task is None


class TestListing:
    async def test_it_answers_with_the_agents_it_holds(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})

        await runner._on_list("nodes/rpi/list", None, _Message(payload=b"{}"))

        (payload,) = [p for t, p, _ in runner.published if t.endswith("/agents")]
        assert [a["name"] for a in payload["agents"]] == ["collector"]


class _Message:
    """An MQTT message as the control plane reads it."""

    def __init__(self, payload: bytes = b"{}") -> None:
        self.payload = payload
        self.properties = None


async def _settle() -> None:
    """Let the tasks a control handler started run to completion."""
    for _ in range(6):
        await asyncio.sleep(0)
