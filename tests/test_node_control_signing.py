"""A node acts only on control messages main signed for it.

A node runs the code a spawn carries and starts every agent a desired state lists,
so anything that can publish to its control topics can run code on it. Main signs
each control message with a key derived for that node, delivered by `/deploy`, and
the node checks the signature before any handler sees the message.

The rule is written twice -- in `wactorz/core/node_signing.py` and in the runner,
which runs on nodes without the package -- so the first tests here hold the two
copies to each other. The rest cover each side, the outbox that has to keep a
signature across a restart, and what main does with what a node reports.
"""

import ast
import asyncio
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest

from wactorz.agents import installer_agent
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.hosts import NodeHost
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import SIGNING_REPUBLISH_INTERVAL_S, NodeManager
from wactorz.config import DeployTarget
from wactorz.core import node_signing
from wactorz.core.actor import Actor, Message
from wactorz.core.mqtt import publish_properties
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.node import runner as node_runner
from wactorz.node import signing as node_signing_side

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test is an install of its own: its own state directory and signing secret."""
    state = tmp_path / "state"
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
    monkeypatch.setattr(node_signing, "_secret", None)
    monkeypatch.setattr(node_signing, "_last_sequence", None)
    return state


def _signed(topic: str, payload: bytes) -> dict[str, str]:
    pairs = node_signing.node_control_properties(topic, payload)
    assert pairs is not None
    return dict(pairs)


def _guard(
    tmp_path: Path, node: str = "rpi", mode: str = "enforce", since: str = ""
) -> node_signing_side.ControlGuard:
    node_dir = tmp_path / f"node-{node}"
    node_dir.mkdir(exist_ok=True)
    return node_signing_side.ControlGuard(node_signing.node_key(node), since, mode, str(node_dir))


# ── Both sides read one rule ───────────────────────────────────────────────────


class TestBothSidesFollowOneRule:
    """Main signs and a node checks, from the same module.

    There used to be two copies of this rule, because the node ran a file that
    could not import the package. It can now, so what these check is that the
    receiving side really does read the shared definitions rather than having
    grown its own that agree for the moment.
    """

    def test_the_node_reads_the_shared_control_topics(self) -> None:
        assert node_runner.CONTROL_LEAVES is node_signing.CONTROL_LEAVES

    def test_the_node_reads_the_shared_properties(self) -> None:
        assert node_signing_side.SEQUENCE_PROPERTY is node_signing.SEQUENCE_PROPERTY
        assert node_signing_side.SIGNATURE_PROPERTY is node_signing.SIGNATURE_PROPERTY

    def test_the_node_signs_over_the_shared_bytes(self) -> None:
        assert node_signing_side.signing_input is node_signing.signing_input

    def test_what_main_signs_a_node_accepts(self, tmp_path: Path) -> None:
        payload = b'{"name": "collector", "code": "async def process(agent): pass"}'
        properties = _signed("nodes/rpi/spawn", payload)
        assert _guard(tmp_path).admit("spawn", "nodes/rpi/spawn", payload, properties)


# ── Main signs ─────────────────────────────────────────────────────────────────


class TestMainSigns:
    def test_only_control_topics_are_signed(self) -> None:
        assert node_signing.node_control_properties("agents/x/logs", b"{}") is None
        assert node_signing.node_control_properties("nodes/rpi/heartbeat", b"{}") is None
        assert node_signing.node_control_properties("nodes/rpi/extra/spawn", b"{}") is None

    def test_an_empty_payload_is_not_signed(self) -> None:
        # Clearing a retained message instructs nothing.
        assert node_signing.node_control_properties("nodes/rpi/desired_state", b"") is None

    def test_text_is_signed_as_the_bytes_it_is_sent_as(self, tmp_path: Path) -> None:
        text = '{"name": "δοκιμή"}'
        properties = _signed("nodes/rpi/stop", text.encode())
        # Signed from the str, checked against the bytes paho sends.
        pairs = node_signing.node_control_properties("nodes/rpi/stop", text)
        assert pairs is not None
        assert _guard(tmp_path).admit("stop", "nodes/rpi/stop", text.encode(), dict(pairs))
        assert properties[node_signing.SIGNATURE_PROPERTY]

    def test_sequence_numbers_only_increase(self) -> None:
        first = node_signing.next_sequence()
        second = node_signing.next_sequence()
        assert second > first

    def test_a_restart_with_a_clock_that_stepped_back_does_not_reuse_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued = node_signing.next_sequence()
        monkeypatch.setattr(node_signing, "_last_sequence", None)  # a restart
        monkeypatch.setattr(node_signing.time, "time_ns", lambda: 1_000_000)  # 1970
        assert node_signing.next_sequence() > issued

    def test_each_node_has_its_own_key(self) -> None:
        assert node_signing.node_key("rpi") != node_signing.node_key("nuc")

    def test_the_key_survives_a_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = node_signing.node_key("rpi")
        monkeypatch.setattr(node_signing, "_secret", None)
        assert node_signing.node_key("rpi") == key

    def test_the_secret_is_readable_by_its_owner_only(self, _fresh_install: Path) -> None:
        node_signing.node_key("rpi")
        mode = stat.S_IMODE((_fresh_install / node_signing.KEY_FILE).stat().st_mode)
        if os.name != "nt":
            assert mode == 0o600

    def test_a_damaged_secret_sends_unsigned_and_says_so(
        self, _fresh_install: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fresh_install.mkdir(parents=True, exist_ok=True)
        (_fresh_install / node_signing.KEY_FILE).write_text("not a key", encoding="ascii")
        assert node_signing.node_control_properties("nodes/rpi/stop", b"{}") is None
        assert "Could not sign" in caplog.text

    def test_aiomqtt_gets_the_signature_as_properties(self) -> None:
        kwargs = node_signing.signed_publish_kwargs("nodes/rpi/stop_all", '{"reason": "x"}')
        names = [name for name, _value in kwargs["properties"].UserProperty]
        assert names == [node_signing.SEQUENCE_PROPERTY, node_signing.SIGNATURE_PROPERTY]
        assert node_signing.signed_publish_kwargs("agents/x/logs", "{}") == {}


# ── A node checks ──────────────────────────────────────────────────────────────


class TestANodeWithoutAKey:
    def test_it_acts_on_everything_as_it_always_did(self, tmp_path: Path) -> None:
        guard = node_signing_side.ControlGuard("", "", "", str(tmp_path))
        assert guard.admit("spawn", "nodes/rpi/spawn", b"{}", {})
        assert guard.mode == "off"
        assert guard.failures == 0


class TestANodeThatEnforces:
    def test_a_tampered_payload_is_refused(self, tmp_path: Path) -> None:
        properties = _signed("nodes/rpi/spawn", b'{"code": "pass"}')
        guard = _guard(tmp_path)
        assert not guard.admit("spawn", "nodes/rpi/spawn", b'{"code": "evil()"}', properties)
        assert guard.failures == 1

    def test_a_message_signed_for_another_node_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"code": "pass"}'
        properties = _signed("nodes/nuc/spawn", payload)
        # Delivered to rpi's topic: rpi's key did not make it.
        assert not _guard(tmp_path, node="rpi").admit(
            "spawn", "nodes/rpi/spawn", payload, properties
        )

    def test_a_message_signed_as_another_command_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"name": "a"}'
        properties = _signed("nodes/rpi/restart_agent", payload)
        assert not _guard(tmp_path).admit("stop", "nodes/rpi/stop", payload, properties)

    def test_an_unsigned_message_is_refused(self, tmp_path: Path) -> None:
        assert not _guard(tmp_path).admit("stop_all", "nodes/rpi/stop_all", b"{}", {})

    @pytest.mark.parametrize("sequence", ["", "12a", "１２", "-5"])
    def test_a_sequence_that_is_not_a_number_is_refused(
        self, tmp_path: Path, sequence: str
    ) -> None:
        properties = _signed("nodes/rpi/stop", b"{}")
        properties[node_signing.SEQUENCE_PROPERTY] = sequence
        assert not _guard(tmp_path).admit("stop", "nodes/rpi/stop", b"{}", properties)

    def test_a_message_is_acted_on_once(self, tmp_path: Path) -> None:
        payload = b'{"name": "a"}'
        properties = _signed("nodes/rpi/stop", payload)
        guard = _guard(tmp_path)
        assert guard.admit("stop", "nodes/rpi/stop", payload, properties)
        assert not guard.admit("stop", "nodes/rpi/stop", payload, properties)

    def test_a_replay_after_the_node_restarts_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"name": "a"}'
        properties = _signed("nodes/rpi/stop", payload)
        assert _guard(tmp_path).admit("stop", "nodes/rpi/stop", payload, properties)
        restarted = _guard(tmp_path)
        assert not restarted.admit("stop", "nodes/rpi/stop", payload, properties)

    def test_a_message_from_before_the_deploy_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"name": "a"}'
        captured = _signed("nodes/rpi/stop", payload)
        since = str(node_signing.next_sequence())  # the deploy happens after it
        assert not _guard(tmp_path, since=since).admit("stop", "nodes/rpi/stop", payload, captured)

    def test_one_published_after_the_deploy_is_accepted(self, tmp_path: Path) -> None:
        since = str(node_signing.next_sequence())
        payload = b'{"name": "a"}'
        assert _guard(tmp_path, since=since).admit(
            "stop", "nodes/rpi/stop", payload, _signed("nodes/rpi/stop", payload)
        )

    def test_two_messages_may_arrive_out_of_order(self, tmp_path: Path) -> None:
        # Main publishes over more than one connection.
        earlier = _signed("nodes/rpi/stop", b'{"name": "a"}')
        later = _signed("nodes/rpi/stop", b'{"name": "b"}')
        guard = _guard(tmp_path)
        assert guard.admit("stop", "nodes/rpi/stop", b'{"name": "b"}', later)
        assert guard.admit("stop", "nodes/rpi/stop", b'{"name": "a"}', earlier)

    def test_the_retained_desired_state_is_applied_again_after_a_reboot(
        self, tmp_path: Path
    ) -> None:
        payload = b'{"agents": []}'
        properties = _signed("nodes/rpi/desired_state", payload)
        assert _guard(tmp_path).admit(
            "desired_state", "nodes/rpi/desired_state", payload, properties
        )
        rebooted = _guard(tmp_path)
        assert rebooted.admit("desired_state", "nodes/rpi/desired_state", payload, properties)

    def test_an_older_desired_state_is_refused(self, tmp_path: Path) -> None:
        old = _signed("nodes/rpi/desired_state", b'{"agents": ["gone"]}')
        new = _signed("nodes/rpi/desired_state", b'{"agents": []}')
        guard = _guard(tmp_path)
        assert guard.admit("desired_state", "nodes/rpi/desired_state", b'{"agents": []}', new)
        assert not guard.admit(
            "desired_state", "nodes/rpi/desired_state", b'{"agents": ["gone"]}', old
        )

    def test_a_desired_state_from_before_the_deploy_is_still_applied(self, tmp_path: Path) -> None:
        payload = b'{"agents": []}'
        properties = _signed("nodes/rpi/desired_state", payload)
        since = str(node_signing.next_sequence())
        assert _guard(tmp_path, since=since).admit(
            "desired_state", "nodes/rpi/desired_state", payload, properties
        )

    def test_it_reports_itself_as_enforcing(self, tmp_path: Path) -> None:
        assert _guard(tmp_path).mode == "enforce"


class TestANodeThatWarns:
    def test_it_acts_on_a_bad_message_and_counts_it(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode="warn")
        assert guard.admit("stop", "nodes/rpi/stop", b"{}", {})
        assert guard.failures == 1
        assert guard.mode == "warn"

    def test_an_unrecognised_mode_warns(self, tmp_path: Path) -> None:
        assert _guard(tmp_path, mode="enforc").mode == "warn"


class TestANodeWithAnUnreadableKey:
    def test_it_refuses_everything_rather_than_trusting_everyone(self, tmp_path: Path) -> None:
        guard = node_signing_side.ControlGuard("not hex", "", "warn", str(tmp_path))
        assert guard.mode == "invalid"
        assert not guard.admit("spawn", "nodes/rpi/spawn", b"{}", {})


class TestWhatTheRunnerChecks:
    def _runner(self, tmp_path: Path) -> node_runner.NodeRunner:
        runner = node_runner.NodeRunner.__new__(node_runner.NodeRunner)
        runner.node_name = "rpi"
        runner._control = _guard(tmp_path)
        return runner

    def test_a_signed_message_carries_its_signature_in_its_properties(self, tmp_path: Path) -> None:
        payload = b'{"name": "a"}'
        pairs = node_signing.node_control_properties("nodes/rpi/stop", payload)
        assert pairs is not None
        message = SimpleNamespace(payload=payload, properties=publish_properties(pairs))
        assert self._runner(tmp_path)._admit_control("nodes/rpi/stop", message)

    def test_clearing_a_retained_spawn_or_desired_state_needs_no_signature(
        self, tmp_path: Path
    ) -> None:
        runner = self._runner(tmp_path)
        for leaf in ("spawn", "desired_state"):
            message = SimpleNamespace(payload=b"", properties=None)
            assert runner._admit_control(f"nodes/rpi/{leaf}", message)

    def test_an_empty_stop_all_still_needs_one(self, tmp_path: Path) -> None:
        # Its handler shuts the node down whatever the payload holds.
        message = SimpleNamespace(payload=b"", properties=None)
        assert not self._runner(tmp_path)._admit_control("nodes/rpi/stop_all", message)

    def test_traffic_that_is_not_control_is_not_checked(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        message = SimpleNamespace(payload=b"{}", properties=None)
        assert runner._admit_control("nodes/rpi/reply/abc", message)
        assert runner._admit_control("agents/by-name/collector/task", message)

    def test_the_heartbeat_says_how_the_node_checks(self, tmp_path: Path) -> None:
        guard = _guard(tmp_path, mode="warn")
        guard.admit("stop", "nodes/rpi/stop", b"{}", {})
        assert (guard.mode, guard.failures) == ("warn", 1)


# ── The outbox keeps a signature ───────────────────────────────────────────────


#: User properties as a signed message carries them.
_PROPERTIES = [(node_signing.SEQUENCE_PROPERTY, "1"), (node_signing.SIGNATURE_PROPERTY, "ab")]


class TestTheOutboxKeepsTheSignature:
    async def test_a_queued_message_replays_with_its_properties(self, tmp_path: Path) -> None:
        db = str(tmp_path / "outbox.db")
        first = MQTTPublisher(db_path=db)
        first._init_db()
        first._available = True
        await first.publish("nodes/rpi/spawn", "{}", qos=1, user_properties=_PROPERTIES)

        second = MQTTPublisher(db_path=db)  # after a restart
        second._load_pending_from_db()

        *_, user_properties = second._queue.get_nowait()
        assert user_properties == _PROPERTIES

    async def test_an_outbox_from_before_properties_is_upgraded(self, tmp_path: Path) -> None:
        db = tmp_path / "outbox.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, "
                "payload TEXT NOT NULL, retain INTEGER NOT NULL DEFAULT 0, "
                "qos INTEGER NOT NULL DEFAULT 1, ts REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO outbox (topic, payload, retain, qos, ts) "
                "VALUES ('nodes/rpi/stop', '{}', 0, 1, 0)"
            )
            conn.commit()

        publisher = MQTTPublisher(db_path=str(db))
        publisher._init_db()  # as create() does, before it replays
        publisher._load_pending_from_db()

        topic, _payload, _retain, _qos, _row, user_properties = publisher._queue.get_nowait()
        assert (topic, user_properties) == ("nodes/rpi/stop", None)

    async def test_the_drain_loop_sends_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RecordingClient()
        monkeypatch.setattr("wactorz.core.mqtt.mqtt_client", _Connection(client))
        publisher = await MQTTPublisher.create("broker", 1883, db_path=str(tmp_path / "o.db"))
        try:
            await publisher.publish("agents/x/logs", "{}")
            await publisher.publish("nodes/rpi/stop", "{}", qos=1, user_properties=_PROPERTIES)
            await asyncio.wait_for(client.twice.wait(), timeout=5.0)
        finally:
            await publisher.disconnect()

        sent = dict(client.calls)
        assert sent["agents/x/logs"] == {}
        assert sent["nodes/rpi/stop"]["properties"].UserProperty == _PROPERTIES


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.twice = asyncio.Event()

    async def publish(
        self, topic: str, payload: object, retain: bool = False, qos: int = 0, **kwargs: Any
    ) -> None:
        self.calls.append((topic, kwargs))
        if len(self.calls) >= 2:
            self.twice.set()


class _Connection:
    def __init__(self, client: _RecordingClient) -> None:
        self._client = client

    def __call__(self, _broker: str, _port: int, **_kwargs: Any) -> "_Connection":
        return self

    async def __aenter__(self) -> _RecordingClient:
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# ── Who signs ──────────────────────────────────────────────────────────────────


class _Quiet(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


class _PublishRecorder:
    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def publish(
        self, topic: str, payload: object, retain: bool = False, qos: int = 0, **kwargs: Any
    ) -> None:
        self.kwargs.append(kwargs)


class TestWhoSigns:
    async def test_an_agent_publishing_to_a_node_is_not_signed(self, tmp_path: Path) -> None:
        # An agent's publish goes through Actor, which signs nothing: an agent that
        # addressed a node's spawn topic must not reach it as main does.
        actor = _Quiet(name="agent", persistence_dir=str(tmp_path))
        recorder = _PublishRecorder()
        actor._mqtt_client = recorder  # pyright: ignore[reportAttributeAccessIssue]
        await actor._mqtt_publish("nodes/rpi/spawn", {"code": "pass"}, qos=1)
        assert recorder.kwargs == [{}]

    async def test_properties_are_passed_on_when_there_are_some(self, tmp_path: Path) -> None:
        class _Signing(_Quiet):
            def _publish_properties(self, topic: str, encoded: Any) -> list[tuple[str, str]]:
                return [("k", "v")]

        actor = _Signing(name="signing", persistence_dir=str(tmp_path))
        recorder = _PublishRecorder()
        actor._mqtt_client = recorder  # pyright: ignore[reportAttributeAccessIssue]
        await actor._mqtt_publish("nodes/rpi/stop", {"name": "a"}, qos=1)
        assert recorder.kwargs == [{"user_properties": [("k", "v")]}]

    def test_main_signs_what_it_addresses_to_a_node(self, tmp_path: Path) -> None:
        encoded = '{"name": "a"}'
        pairs = MainActor._publish_properties(cast("MainActor", None), "nodes/rpi/stop", encoded)
        assert pairs is not None
        assert _guard(tmp_path).admit("stop", "nodes/rpi/stop", encoded.encode(), dict(pairs))
        assert MainActor._publish_properties(cast("MainActor", None), "agents/x/logs", "{}") is None

    def test_every_other_control_publish_is_signed(self) -> None:
        """A publish outside main's own, to a node's control topic, carries a signature.

        Read from source: those publishes need a live broker connection and a
        missing main. The topic has to be written in place for this to see it,
        which is how every such publish here is written.
        """
        found, unsigned = _control_publishes()
        assert found >= 2, "the scan found none of the publishes it exists to check"
        assert unsigned == []


def _control_publishes() -> tuple[int, list[str]]:
    found = 0
    unsigned: list[str] = []
    for path in sorted((ROOT / "wactorz").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "publish"
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)
            ):
                continue
            parts = node.args[0].values
            head, tail = parts[0], parts[-1]
            if not (
                isinstance(head, ast.Constant)
                and str(head.value).startswith("nodes/")
                and isinstance(tail, ast.Constant)
                and str(tail.value).count("/") == 1
                and str(tail.value).lstrip("/") in node_signing.CONTROL_LEAVES
            ):
                continue
            payload = node.args[1] if len(node.args) > 1 else None
            if isinstance(payload, ast.Constant) and payload.value == b"":
                continue
            found += 1
            if not any(
                keyword.arg is None
                and isinstance(keyword.value, ast.Call)
                and isinstance(keyword.value.func, ast.Name)
                and keyword.value.func.id == "signed_publish_kwargs"
                for keyword in node.keywords
            ):
                unsigned.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found, unsigned


# ── Deploy delivers the key ────────────────────────────────────────────────────


class _Sftp:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def open(self, path: str, _mode: str) -> Any:
        written = self.written

        class _Handle:
            async def write(self, body: str) -> None:
                written[path] = body

            async def __aenter__(self) -> "_Handle":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return _Handle()

    async def chmod(self, _path: str, _mode: int) -> None:
        return None


class TestDeployDeliversTheKey:
    async def test_the_nodes_env_carries_its_key_start_and_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer_agent, "NODE_SIGNING", "enforce")
        agent = InstallerAgent.__new__(InstallerAgent)
        agent.name = "installer"
        sftp = _Sftp()

        await agent._put_node_env(
            sftp, DeployTarget(name="rpi", host="10.0.0.5"), "/home/pi", "rpi", "10.0.0.1", 1883
        )

        env = dict(
            line.split("=", 1) for line in sftp.written["/home/pi/wactorz/.env"].splitlines()
        )
        assert env["WACTORZ_NODE_KEY"] == node_signing.node_key("rpi")
        assert int(env["WACTORZ_CONTROL_SINCE"]) > 0
        assert env["WACTORZ_NODE_SIGNING"] == "enforce"


# ── What main does with what a node reports ────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.notices: list[dict[str, Any]] = []
        self.republished: list[str] = []

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notices.append(notice)

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.republished.append(node)


def _nodes() -> tuple[NodeManager, _Host]:
    host = _Host()
    manager = NodeManager(cast("NodeHost", host), cast("ManifestRegistry", object()))
    return manager, host


def _report(mode: str, failures: int = 0) -> dict[str, Any]:
    return {"signing": mode, "signing_failures": failures}


class TestMainFollowsSigning:
    async def test_a_heartbeat_records_how_the_node_checks(self) -> None:
        nodes = NodeManager()
        await nodes.receive_heartbeat(
            "rpi", {"agents": [], "signing": "warn", "signing_failures": 2}
        )
        assert (nodes.known["rpi"]["signing"], nodes.known["rpi"]["signing_failures"]) == (
            "warn",
            2,
        )

    async def test_a_heartbeat_from_before_signing_reads_as_off(self) -> None:
        nodes = NodeManager()
        await nodes.receive_heartbeat("rpi", {"agents": []})
        assert (nodes.known["rpi"]["signing"], nodes.known["rpi"]["signing_failures"]) == ("off", 0)

    async def test_a_node_that_starts_checking_gets_a_signed_desired_state(self) -> None:
        nodes, host = _nodes()
        await nodes.follow_signing("rpi", _report("enforce"), None)
        await nodes.follow_signing("rpi", _report("enforce"), _report("enforce"))
        assert host.republished == ["rpi"]
        assert host.notices == []

    async def test_a_node_that_does_not_check_is_left_alone(self) -> None:
        nodes, host = _nodes()
        await nodes.follow_signing("rpi", _report("off"), None)
        assert (host.republished, host.notices) == ([], [])

    async def test_new_failures_are_said_in_chat(self) -> None:
        nodes, host = _nodes()
        await nodes.follow_signing("rpi", _report("enforce", 3), _report("enforce", 1))
        assert len(host.notices) == 1
        assert "2 command(s)" in host.notices[0]["message"]
        assert "refused" in host.notices[0]["message"]

    async def test_a_restarted_node_counts_from_zero(self) -> None:
        nodes, host = _nodes()
        await nodes.follow_signing("rpi", _report("warn", 1), _report("warn", 5))
        assert "1 command(s)" in host.notices[0]["message"]
        assert "acted on" in host.notices[0]["message"]

    async def test_republishing_is_not_repeated_within_the_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nodes, host = _nodes()
        clock = [1000.0]
        monkeypatch.setattr("wactorz.agents.main.nodes.time.monotonic", lambda: clock[0])
        await nodes.follow_signing("rpi", _report("enforce", 1), _report("enforce", 0))
        await nodes.follow_signing("rpi", _report("enforce", 2), _report("enforce", 1))
        clock[0] += SIGNING_REPUBLISH_INTERVAL_S
        await nodes.follow_signing("rpi", _report("enforce", 3), _report("enforce", 2))
        assert host.republished == ["rpi", "rpi"]

    async def test_a_node_that_cannot_read_its_key_is_reported_once(self) -> None:
        nodes, host = _nodes()
        await nodes.follow_signing("rpi", _report("invalid", 1), _report("off"))
        await nodes.follow_signing("rpi", _report("invalid", 2), _report("invalid", 1))
        assert [notice["severity"] for notice in host.notices] == ["critical"]
        assert host.republished == []


class TestTheRecordOfWhatWasAccepted:
    """A node remembers which commands it has acted on, so a replay is refused.

    The record is a file, and a file can be unreadable, truncated or written by
    something else. None of that may stop the node: refusing every command is a
    worse failure than forgetting which ones were already obeyed.
    """

    @staticmethod
    def _with_record(tmp_path: Path, contents: str) -> Any:
        """A guard whose record already holds ``contents``.

        Written into the directory the guard is actually given — put it beside
        that instead and the guard finds no file at all, which every test here
        would pass with while exercising nothing.
        """
        node_dir = tmp_path / "node-rpi"
        node_dir.mkdir(exist_ok=True)
        (node_dir / node_signing_side.SEEN_FILE).write_text(contents, encoding="utf-8")
        guard = _guard(tmp_path)
        assert guard._path.exists(), "the record was written where the guard cannot see it"
        return guard

    def test_a_file_that_will_not_parse_is_started_over(self, tmp_path: Path) -> None:
        guard = self._with_record(tmp_path, "{not json")

        # Read as empty rather than raising, and the command still goes through.
        assert guard._load() == {}
        payload = b'{"name": "collector"}'
        assert guard.admit("spawn", "nodes/rpi/spawn", payload, _signed("nodes/rpi/spawn", payload))

    def test_a_file_holding_something_else_entirely_is_ignored(self, tmp_path: Path) -> None:
        guard = self._with_record(tmp_path, '["not", "a", "map"]')

        assert guard._load() == {}
        payload = b"{}"
        assert guard.admit("stop", "nodes/rpi/stop", payload, _signed("nodes/rpi/stop", payload))

    def test_entries_that_are_not_sequence_numbers_are_dropped(self, tmp_path: Path) -> None:
        # Whatever else is in there, what is kept is a list of integers per
        # topic — anything else would break the comparison that refuses a replay.
        guard = self._with_record(tmp_path, '{"spawn": [1, "two", null, 3], "stop": "not a list"}')

        assert guard._load() == {"spawn": [1, 3]}

    def test_a_record_that_cannot_be_written_does_not_stop_the_node(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Losing the record costs replay protection across a restart, and that
        is worth saying. Refusing the command would cost the command."""
        guard = _guard(tmp_path)
        # A directory where the file goes: writing fails, reading is unaffected.
        guard._path.mkdir(parents=True, exist_ok=True)
        payload = b'{"name": "collector"}'

        with caplog.at_level("WARNING"):
            admitted = guard.admit(
                "spawn", "nodes/rpi/spawn", payload, _signed("nodes/rpi/spawn", payload)
            )

        assert admitted
        assert "could not record" in caplog.text.lower()
