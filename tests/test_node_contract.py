"""The contract between main and a node, pinned.

A node is whatever answers on `nodes/<name>/...`: today the single-file runner,
tomorrow the package running in a node role. Main never sees which; it sees
topics, payload fields and, on the node's own disk, a state file. Those three
are the contract, and this file is where they are written down, so that the
runner and any runtime that replaces it are held to the same one.

Each constant below is the contract as it stands. A change to one is a change
both sides have to make together, which is the point of failing here first.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

import wactorz
from wactorz import remote_runner
from wactorz.agents.main.nodes import DEFAULT_NODE_RUNTIME, NodeManager

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "wactorz"
RUNNER = PACKAGE / "remote_runner.py"

# ── Topics ─────────────────────────────────────────────────────────────────────

#: Main publishes, the node subscribes. Named by the segment after `nodes/<name>/`.
CONTROL_TOPICS = frozenset(
    {
        "spawn",
        "desired_state",
        "stop",
        "stop_all",
        "restart",
        "restart_agent",
        "migrate",
    }
)

#: The node publishes, main subscribes.
REPORT_TOPICS = frozenset(
    {
        "heartbeat",
        "spawn_ack",
        "state_return",
        "migrate_result",
    }
)

#: The node's reply channel. A node addresses a request to main with a
#: `_reply_topic` of its own choosing under here, and main answers on it. Main
#: never spells the topic out, so it is not in the sets above.
REPLY_TOPIC = "reply"

#: Published by the node and read by nobody on the server. `list` asks the
#: node to publish `agents`, and nothing on the server asks; `logs` is not a
#: leaf the dashboard relays. A runtime replacing the runner need not carry
#: them, and a change that puts them to use moves them into REPORT_TOPICS.
UNUSED_TOPICS = frozenset({"list", "agents", "logs"})

NODE_TOPICS = CONTROL_TOPICS | REPORT_TOPICS | {REPLY_TOPIC} | UNUSED_TOPICS

# ── Payloads ───────────────────────────────────────────────────────────────────

#: Fields main reads off a node heartbeat. `version` and `runtime` are the two
#: a node may omit -- an older runner does -- and main fills in for it.
HEARTBEAT_FIELDS = frozenset(
    {
        "node",
        "node_id",
        "timestamp",
        "agents",
        "agent_count",
        "pid",
        "uptime_s",
        "cpu_pct",
        "mem_used_mb",
        "mem_free_mb",
        "version",
        "runtime",
    }
)

#: Spawn config fields the node acts on. Anything else rides along untouched
#: and comes back on a migrate.
SPAWN_CONFIG_FIELDS = frozenset(
    {
        "name",
        "code",
        "poll_interval",
        "max_restarts",
        "restart_delay",
        "install",
        "replace",
        "_initial_state",
        "_migration_token",
    }
)

SPAWN_ACK_FIELDS = frozenset({"agent", "migration_token", "node", "timestamp"})
STATE_RETURN_FIELDS = frozenset(
    {"agent", "return_token", "config", "state", "state_keys_dropped", "from_node", "timestamp"}
)
MIGRATE_RESULT_FIELDS = frozenset({"success", "agent", "timestamp"})

# ── On-disk state ──────────────────────────────────────────────────────────────

#: Where a node keeps an agent's persisted keys, relative to its state
#: directory, and in what form. A runtime that replaces the runner reads and
#: writes this same file, so an upgraded node keeps every agent's memory.
STATE_FILE_SUFFIX = "_state.json"

# ── Helpers ────────────────────────────────────────────────────────────────────

_NODE_TOPIC = re.compile(r"nodes/(?:\{[^}]+\}|\+)/([a-z_]+)")


def _topic_segments(source: str) -> set[str]:
    return set(_NODE_TOPIC.findall(source))


def _server_sources() -> list[Path]:
    return [p for p in PACKAGE.rglob("*.py") if p != RUNNER]


class _Runner:
    node_name = "rpi"


def _runner_with_capture() -> tuple[remote_runner._RemoteRunner, list[tuple[str, Any]]]:
    """A runner with only what the node heartbeat loop touches."""
    runner = remote_runner._RemoteRunner.__new__(remote_runner._RemoteRunner)
    runner.node_name = "rpi"
    runner.broker = "localhost"
    runner._agents = {}
    runner._start_time = 0.0
    runner._running = True
    published: list[tuple[str, Any]] = []

    async def publish(topic: str, data: Any, retain: bool = False) -> None:
        published.append((topic, data))
        runner._running = False

    runner.publish = publish  # type: ignore[method-assign]
    return runner, published


# ── Topics: both sides name the same set ───────────────────────────────────────


class TestTopics:
    def test_the_runner_speaks_exactly_the_contract_topics(self) -> None:
        found = _topic_segments(RUNNER.read_text(encoding="utf-8"))
        assert found == NODE_TOPICS

    def test_main_speaks_only_contract_topics(self) -> None:
        found: set[str] = set()
        for path in _server_sources():
            found |= _topic_segments(path.read_text(encoding="utf-8"))
        assert found <= NODE_TOPICS, found - NODE_TOPICS

    def test_every_control_topic_is_published_by_main(self) -> None:
        found: set[str] = set()
        for path in _server_sources():
            found |= _topic_segments(path.read_text(encoding="utf-8"))
        assert found >= CONTROL_TOPICS, CONTROL_TOPICS - found

    def test_every_report_topic_is_read_by_main(self) -> None:
        found: set[str] = set()
        for path in _server_sources():
            found |= _topic_segments(path.read_text(encoding="utf-8"))
        assert found >= REPORT_TOPICS, REPORT_TOPICS - found

    def test_main_answers_on_the_reply_topic_the_node_chose(self) -> None:
        # The runner mints reply topics under its own name; main publishes to
        # whatever `_reply_topic` the request carried. Neither side may spell
        # the other's half.
        runner = RUNNER.read_text(encoding="utf-8")
        assert f'f"nodes/{{self._agent.node_name}}/{REPLY_TOPIC}/' in runner
        bridge = (PACKAGE / "agents" / "main" / "llm_bridge.py").read_text(encoding="utf-8")
        assert 'data.get("_reply_topic")' in bridge

    def test_the_unused_topics_stay_unused_until_someone_reads_them(self) -> None:
        found: set[str] = set()
        for path in _server_sources():
            found |= _topic_segments(path.read_text(encoding="utf-8"))
        assert not (UNUSED_TOPICS & found), UNUSED_TOPICS & found


# ── Heartbeat ──────────────────────────────────────────────────────────────────


class TestHeartbeat:
    async def test_the_runner_sends_every_contract_field(self) -> None:
        runner, published = _runner_with_capture()

        await asyncio.wait_for(runner._node_heartbeat_loop(interval=0), timeout=2.0)

        assert len(published) == 1
        topic, payload = published[0]
        assert topic == "nodes/rpi/heartbeat"
        assert set(payload) >= HEARTBEAT_FIELDS, HEARTBEAT_FIELDS - set(payload)

    async def test_the_runner_names_its_version_and_runtime(self) -> None:
        runner, published = _runner_with_capture()

        await asyncio.wait_for(runner._node_heartbeat_loop(interval=0), timeout=2.0)

        _, payload = published[0]
        assert payload["version"] == wactorz.__version__
        assert payload["runtime"] == remote_runner.NODE_RUNTIME == "runner"

    def test_the_runner_version_is_the_package_version(self) -> None:
        # The file ships alone, so it carries a copy; sync_versions stamps it.
        assert wactorz.__version__ == remote_runner.RUNNER_VERSION

    def test_transitional_heartbeats_carry_the_identity_too(self) -> None:
        runner, _ = _runner_with_capture()

        identity = runner._node_identity()

        assert identity == {
            "node": "rpi",
            "version": wactorz.__version__,
            "runtime": "runner",
        }

    async def test_main_records_version_and_runtime(self) -> None:
        nodes = NodeManager()

        await nodes.receive_heartbeat("rpi", {"agents": [], "version": "9.9.9", "runtime": "node"})

        (entry,) = nodes.list_nodes()
        assert entry["version"] == "9.9.9"
        assert entry["runtime"] == "node"

    async def test_main_tolerates_a_heartbeat_without_them(self) -> None:
        # A runner older than the fields says nothing; main must not treat
        # that as an error, and must not mistake it for the new runtime.
        nodes = NodeManager()

        await nodes.receive_heartbeat("rpi", {"agents": ["a"]})

        (entry,) = nodes.list_nodes()
        assert entry["version"] is None
        assert entry["runtime"] == DEFAULT_NODE_RUNTIME == "runner"
        assert entry["agents"] == ["a"]


# ── Spawn config and the replies a node sends ──────────────────────────────────


class TestPayloadShapes:
    def test_the_runner_reads_only_contract_spawn_fields(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        found = set(re.findall(r'config\.(?:get|pop)\("([a-z_]+)"', source))
        assert found == SPAWN_CONFIG_FIELDS, found ^ SPAWN_CONFIG_FIELDS

    @pytest.mark.parametrize(
        ("topic", "fields"),
        [
            ("spawn_ack", SPAWN_ACK_FIELDS),
            ("state_return", STATE_RETURN_FIELDS),
            ("migrate_result", MIGRATE_RESULT_FIELDS),
        ],
    )
    def test_the_runner_publishes_the_contract_fields(self, topic: str, fields: frozenset) -> None:
        # Every literal payload published on the topic names at least these
        # keys. Read from source: the paths that publish them need a broker
        # and a running agent, and the shape is what is being pinned.
        source = RUNNER.read_text(encoding="utf-8")
        pattern = re.compile(
            r'f"nodes/\{self\.node_name\}/' + topic + r'",\s*\{(.*?)\n\s*\}', re.DOTALL
        )
        bodies = pattern.findall(source)
        assert bodies, f"no literal payload published on {topic}"
        for body in bodies:
            keys = set(re.findall(r'"([a-z_]+)":', body))
            assert fields <= keys, (topic, fields - keys)


# ── State file ─────────────────────────────────────────────────────────────────


class TestStateFile:
    def test_the_file_lives_under_the_agent_name_as_json(self, tmp_path: Path) -> None:
        agent = remote_runner._RemoteAgent(
            {"name": "temp-sensor", "code": ""}, _Runner(), state_dir=str(tmp_path)
        )

        agent._persistent_state = {"readings": [1, 2], "note": "δοκιμή"}
        agent._save_state()

        path = tmp_path / f"temp-sensor{STATE_FILE_SUFFIX}"
        assert agent._state_path == path
        assert json.loads(path.read_text(encoding="utf-8")) == agent._persistent_state

    def test_a_name_with_path_separators_stays_in_the_directory(self, tmp_path: Path) -> None:
        agent = remote_runner._RemoteAgent(
            {"name": "a/b\\c", "code": ""}, _Runner(), state_dir=str(tmp_path)
        )

        assert agent._state_path.parent == tmp_path
        assert agent._state_path.name == f"a_b_c{STATE_FILE_SUFFIX}"

    def test_a_fresh_agent_reads_what_the_last_one_wrote(self, tmp_path: Path) -> None:
        # The upgrade path in one test: whatever runtime wrote the file, the
        # next one to start on this node finds the agent's memory intact.
        first = remote_runner._RemoteAgent(
            {"name": "temp-sensor", "code": ""}, _Runner(), state_dir=str(tmp_path)
        )
        first._persistent_state = {"count": 3}
        first._save_state()

        second = remote_runner._RemoteAgent(
            {"name": "temp-sensor", "code": ""}, _Runner(), state_dir=str(tmp_path)
        )

        assert second._persistent_state == {"count": 3}
