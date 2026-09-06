"""Client ids must be stable across reconnects and unique per connection.

A durable MQTT session is addressed by client id. Two connections sharing one
make the broker drop the older, which it then reconnects, which drops the
newer -- a kick-loop that never settles. The publisher used to pin the literal
`wactorz-publisher`, so two Wactorz servers against one broker did exactly that.
"""

import ast
import uuid
from pathlib import Path

import pytest

from wactorz.agents.dynamic.listener import SubscriptionHub
from wactorz.core import mqtt
from wactorz.core.actor import derive_actor_id, has_derived_id
from wactorz.core.mqtt import (
    AGENT_SESSION_EXPIRY_SECONDS,
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
)
from wactorz.core.mqtt_publisher import MQTTPublisher

#: Connections that serve one request and must keep a random id -- see
#: TestTheEphemeralSet for why a stable one would be actively harmful.
#: The node's own one-shots, named by the function that opens them. These build
#: `aiomqtt.Client` directly -- remote_runner.py imports nothing from wactorz --
#: so the module scan below cannot see them.
NODE_EPHEMERAL = ["_listen", "_fetch"]

EPHEMERAL = [
    "wactorz/web/chat.py",
    "wactorz/interfaces/chat/cli.py",
    "wactorz/agents/dynamic/messaging.py",
    "wactorz/agents/planner/context.py",
    "wactorz/core/topic_bus.py",
    "wactorz/agents/dynamic/streams.py",
]


def _node_client_calls(function_name: str) -> list[ast.Call]:
    """Every `aiomqtt.Client(...)` built inside one runner function."""
    tree = ast.parse(Path("wactorz/remote_runner.py").read_text(encoding="utf-8"))
    return [
        call
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function_name
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "Client"
    ]


@pytest.fixture(name="state_dir")
def state_dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the install id at a scratch directory, and clear the process cache.

    The cache is module-level by design -- it is read on the connect path of
    every long-lived listener -- so a test that does not reset it measures the
    previous test's install.
    """
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mqtt, "_install_id", None)
    return tmp_path


class TestInstallId:
    def test_it_is_stable_within_a_process(self, state_dir: Path) -> None:
        assert install_id() == install_id()

    def test_it_survives_a_restart(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The whole point: a client that reconnects under a new id abandons its
        # session, so a restart must reuse the id on disk rather than mint one.
        first = install_id()
        monkeypatch.setattr(mqtt, "_install_id", None)

        assert install_id() == first

    def test_two_installs_do_not_collide(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This is the kick-loop fix. Two servers against one broker used to pick
        # the same client id and disconnect each other for ever.
        first = install_id()
        other = tmp_path / "second-install"
        other.mkdir()
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(other))
        monkeypatch.setattr(mqtt, "_install_id", None)

        assert install_id() != first

    def test_a_racing_process_adopts_the_winners_id(self, state_dir: Path) -> None:
        # O_CREAT|O_EXCL: the loser reads rather than writing its own, so two
        # processes starting together cannot disagree about who they are.
        written = state_dir / mqtt.INSTALL_ID_FILE
        written.write_text("deadbeefcafe", encoding="utf-8")

        assert install_id() == "deadbeefcafe"

    def test_it_is_written_without_a_trailing_newline_problem(self, state_dir: Path) -> None:
        # Written as bytes: os.open is text-mode on Windows, and a text-mode
        # fdopen on top of that translates the newline a second time.
        minted = install_id()
        raw = (state_dir / mqtt.INSTALL_ID_FILE).read_bytes()

        assert b"\r" not in raw
        assert raw.decode().strip() == minted


class TestClientId:
    def test_it_has_the_documented_shape(self) -> None:
        assert client_id("pub", "abc123") == "wactorz-pub-abc123"
        assert client_id("srv", "abc123", "llm") == "wactorz-srv-abc123-llm"

    def test_a_detail_separates_connections_sharing_a_role(self) -> None:
        # Main runs six long-lived listeners in one process; two sharing an id
        # is the kick-loop, inside a single server.
        details = ["nodes", "llm", "migration", "delegation", "manifests", "samples"]
        ids = {client_id("srv", "abc123", d) for d in details}

        assert len(ids) == len(details)


class TestThePublisher:
    def test_it_no_longer_pins_a_shared_literal(self, state_dir: Path) -> None:
        publisher = MQTTPublisher(db_path=str(state_dir / "outbox.db"))

        assert publisher.client_id != "wactorz-publisher"
        assert publisher.client_id == f"wactorz-pub-{install_id()}"

    def test_two_installs_get_different_publisher_ids(
        self, state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = MQTTPublisher(db_path=str(state_dir / "outbox.db")).client_id
        other = tmp_path / "second-install"
        other.mkdir()
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(other))
        monkeypatch.setattr(mqtt, "_install_id", None)

        assert MQTTPublisher(db_path=str(other / "outbox.db")).client_id != first

    def test_constructing_one_touches_no_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AGENTS.md: creating directories and opening files belong in a start
        # method, so an object can be constructed in a test without touching
        # anything. Minting the id writes to the state directory, so it waits
        # until the id is actually asked for.
        unused = tmp_path / "never-created"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(unused))
        monkeypatch.setattr(mqtt, "_install_id", None)

        MQTTPublisher(db_path=str(tmp_path / "outbox.db"))

        assert not unused.exists()


class TestAgentScopedSessions:
    """Every connection an agent owns follows the same rule and the same clock.

    Only a name-derived identity can resume a session, and an agent's
    connections age out together rather than by two separate numbers.
    """

    def test_a_named_identity_can_hold_a_session(self) -> None:
        assert has_derived_id("kitchen-sensor", derive_actor_id("kitchen-sensor"))

    def test_an_anonymous_identity_cannot(self) -> None:
        anonymous = str(uuid.uuid4())

        assert not has_derived_id(f"actor-{anonymous[:8]}", anonymous)

    def test_the_hub_and_the_command_listener_share_one_expiry(self) -> None:
        # They belong to the same agent; two numbers would be two things to
        # forget to keep in step.
        assert SubscriptionHub.SESSION_EXPIRY_SECONDS == AGENT_SESSION_EXPIRY_SECONDS

    def test_an_agent_session_is_shorter_than_the_servers(self) -> None:
        # An agent's durability is about surviving a reconnect, not replaying
        # hours of readings that are stale by the time they arrive.
        assert AGENT_SESSION_EXPIRY_SECONDS < SERVER_SESSION_EXPIRY_SECONDS

    def test_the_command_listener_gates_on_the_same_rule(self) -> None:
        source = Path("wactorz/core/actor.py").read_text(encoding="utf-8")

        assert "has_derived_id(self.name, self.actor_id)" in source
        assert "AGENT_SESSION_EXPIRY_SECONDS" in source

    def test_the_actuator_gates_on_the_same_rule(self) -> None:
        source = Path("wactorz/agents/home_assistant_actuator_agent.py").read_text(encoding="utf-8")

        assert "has_derived_id(self.name, str(self.actor_id))" in source
        assert "AGENT_SESSION_EXPIRY_SECONDS" in source


class TestTheEphemeralSet:
    """Request-scoped connections must keep random ids.

    A stable id makes the broker retain a session. Give one to a connection that
    serves a single reply and the broker accumulates sessions for clients that
    never meaningfully return -- and a later reuse of that id receives a stale
    reply meant for someone else. The card names the safe-looking direction,
    making everything durable, as the failure mode to design against, so the set
    is asserted rather than left to reviewer memory.
    """

    @pytest.mark.parametrize("module_path", EPHEMERAL)
    def test_it_passes_no_identifier(self, module_path: str) -> None:
        source = Path(module_path).read_text(encoding="utf-8")
        tree = ast.parse(source)

        named = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "mqtt_client"
            and any(kw.arg == "identifier" for kw in node.keywords)
        ]

        assert not named, f"{module_path} gave a request-scoped connection a stable id"

    @pytest.mark.parametrize("function_name", NODE_EPHEMERAL)
    def test_a_node_one_shot_passes_no_identifier(self, function_name: str) -> None:
        calls = _node_client_calls(function_name)

        assert calls, f"{function_name} builds no client -- has it moved or been renamed?"
        assert not [c for c in calls if any(kw.arg == "identifier" for kw in c.keywords)], (
            f"{function_name} gave a one-shot connection a stable id"
        )

    def test_the_list_is_not_vacuous(self) -> None:
        # A path typo would make every case above pass by finding no calls.
        for module_path in EPHEMERAL:
            tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "mqtt_client"
            ]
            assert calls, f"{module_path} has no mqtt_client call -- has it moved?"
