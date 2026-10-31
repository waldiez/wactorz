"""What the server does with each broker message before browsers see it.

`handle_message` is the half of the broker link with rules — its own docstring
says the loop around it is connection management. It decides what reaches the
dashboard, what is written to `chat_log`, and (the expensive one) when a snapshot
queries the database for totals.

Coverage here is ahead of the authentication work for the same reason as the
`/ws` tests: it lands in this file, and a transport nothing describes is one
where adding a guard looks the same as breaking a feature.

`aiomqtt` is a hard dependency and is never faked in `sys.modules` — that has
produced order-dependent failures before. Nothing here needs a broker, because
the part with rules was deliberately extracted from the part that needs one.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.nodes import NodeManager
from wactorz.web import mqtt, runtime


class _Db:
    """Records chat_log writes instead of holding a database."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write_chat_log(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class _Registry:
    """A registry with no main in it."""

    def all_actors(self) -> list[Any]:
        return []

    def find_by_name(self, _name: str) -> Any:
        return None


@pytest.fixture(name="sent")
def sent_fixture() -> Iterator[list[dict[str, Any]]]:
    """Capture broadcasts and isolate the module globals `handle_message` reads."""
    saved_db, saved_registry = runtime.db, runtime.registry
    saved_reset, saved_agents = runtime.hard_resetting, dict(runtime.state["agents"])
    runtime.db = _Db()  # pyright: ignore[reportAttributeAccessIssue]
    runtime.registry = _Registry()
    runtime.hard_resetting = False
    frames: list[dict[str, Any]] = []

    async def _record(msg: dict[str, Any]) -> None:
        frames.append(msg)

    with patch.object(mqtt.ws, "broadcast", new=_record):
        yield frames

    runtime.db, runtime.registry, runtime.hard_resetting = saved_db, saved_registry, saved_reset
    runtime.state["agents"].clear()
    runtime.state["agents"].update(saved_agents)


@pytest.fixture(name="db")
def db_fixture(sent: list[dict[str, Any]]) -> _Db:
    """The recorder installed by `sent`, typed for assertions."""
    assert isinstance(runtime.db, _Db)
    return runtime.db


def _patches(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("type") == "patch"]


class TestWhatReachesTheBrowser:
    """Only topics the dashboard reads — see `test_relayed_topics` for the set."""

    async def test_a_json_payload_is_forwarded_decoded(self, sent: list[dict[str, Any]]) -> None:
        await mqtt.handle_message("agents/a1/metrics", json.dumps({"cpu": 1.5}))

        relayed = [f for f in sent if f.get("type") == "server_event"]
        assert relayed[0]["payload"] == {"cpu": 1.5}

    async def test_a_payload_that_is_not_json_passes_through_as_text(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # Agents put plain strings on topics too; a payload that will not parse
        # must still be relayed rather than swallowed.
        await mqtt.handle_message("agents/a1/logs", "not json at all")

        relayed = [f for f in sent if f.get("type") == "server_event"]
        assert relayed[0]["payload"] == "not json at all"

    async def test_a_chat_message_comes_with_the_thread_it_is_in(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # The page keeps its threads by id; a voice surface relays the person's
        # words to another agent, which it names only by name.
        runtime.state["agents"].update({"a1": {"name": "reachy"}, "w1": {"name": "weather"}})
        payload = {"from": "user", "to": "weather", "content": "rain today?"}

        await mqtt.handle_message("agents/a1/chat", json.dumps(payload))

        (relayed,) = [f for f in sent if f.get("type") == "server_event"]
        assert relayed["thread_id"] == "w1"

    async def test_anything_else_comes_with_no_thread(self, sent: list[dict[str, Any]]) -> None:
        await mqtt.handle_message("agents/a1/metrics", json.dumps({"cpu": 1.5}))

        (relayed,) = [f for f in sent if f.get("type") == "server_event"]
        assert "thread_id" not in relayed


class _WithMain:
    """A registry holding main, whose node table the dashboard shows nodes from."""

    def __init__(self) -> None:
        self.main = MainActor.__new__(MainActor)
        self.main.nodes = NodeManager()

    def find_by_name(self, name: str) -> Any:
        return self.main if name == "main" else None

    def all_actors(self) -> list[Any]:
        return [self.main]


def _node_frames(frames: list[dict[str, Any]]) -> list[Any]:
    return [
        f["payload"]
        for f in frames
        if f.get("type") == "server_event" and f.get("topic") == "nodes/rpi/heartbeat"
    ]


class TestANodesHeartbeat:
    """Shown as main took it: anything on the broker can publish one."""

    async def test_without_main_it_is_shown_as_it_came(self, sent: list[dict[str, Any]]) -> None:
        await mqtt.handle_message("nodes/rpi/heartbeat", json.dumps({"agents": ["camera"]}))

        assert _node_frames(sent) == [{"agents": ["camera"]}]

    async def test_with_main_the_broker_s_copy_is_not_shown(
        self, sent: list[dict[str, Any]]
    ) -> None:
        runtime.registry = _WithMain()

        await mqtt.handle_message("nodes/rpi/heartbeat", json.dumps({"agents": ["camera"]}))

        assert _node_frames(sent) == []

    async def test_with_main_what_main_took_is_shown(self, sent: list[dict[str, Any]]) -> None:
        registry = _WithMain()
        runtime.registry = registry
        await mqtt.handle_message("nodes/rpi/heartbeat", "{}")

        await registry.main.nodes.receive_heartbeat("rpi", {"agents": ["camera"]})

        assert _node_frames(sent) == [{"agents": ["camera"]}]

    async def test_an_unsigned_one_shows_the_agents_main_knows_not_those_it_claims(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.agents.main.nodes.NODE_SIGNING", "enforce")
        registry = _WithMain()
        runtime.registry = registry
        await mqtt.handle_message("nodes/rpi/heartbeat", "{}")

        await registry.main.nodes.receive_heartbeat(
            "rpi", {"agents": ["camera", "helper"]}, signed=False
        )

        assert _node_frames(sent) == [{"agents": []}]

    async def test_main_is_watched_once_however_many_arrive(
        self, sent: list[dict[str, Any]]
    ) -> None:
        registry = _WithMain()
        runtime.registry = registry

        for _ in range(3):
            await mqtt.handle_message("nodes/rpi/heartbeat", "{}")

        assert len(registry.main.nodes.watchers) == 1


class _Client:
    """A broker client that says, at each subscription, whether main was watched by then."""

    def __init__(self, registry: _WithMain) -> None:
        self.registry = registry
        self.watched_at_subscribe: list[bool] = []

    async def subscribe(self, _topic: str, qos: int = 0) -> None:
        self.watched_at_subscribe.append(bool(self.registry.main.nodes.watchers))

    @property
    def messages(self) -> AsyncIterator[Any]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[Any]:
        # Ends the listener as a shutdown would: it reconnects on anything else.
        raise asyncio.CancelledError
        yield  # pragma: no cover - makes this an async generator


class TestMainIsWatchedFromTheStart:
    """Main reads heartbeats on a client of its own, so the server must be watching first."""

    async def test_watching_begins_when_the_server_connects(
        self, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _WithMain()
        runtime.registry = registry
        client = _Client(registry)

        @asynccontextmanager
        async def _connect(*_args: Any, **_kwargs: Any) -> AsyncIterator[_Client]:
            yield client

        monkeypatch.setattr(mqtt, "mqtt_client", _connect)
        monkeypatch.setattr(runtime, "MQTT_TOPICS", ["nodes/+/heartbeat"])

        with pytest.raises(asyncio.CancelledError):
            await mqtt.mqtt_listener()

        assert client.watched_at_subscribe == [True]

    async def test_a_heartbeat_main_took_before_it_was_watched_is_shown(
        self, sent: list[dict[str, Any]]
    ) -> None:
        registry = _WithMain()
        runtime.registry = registry
        await registry.main.nodes.receive_heartbeat("rpi", {"agents": ["camera"]})

        await mqtt.handle_message("nodes/rpi/heartbeat", json.dumps({"agents": ["claimed"]}))

        shown = _node_frames(sent)
        assert [frame["agents"] for frame in shown] == [["camera"]]

    async def test_what_main_held_is_shown_once_however_many_follow(
        self, sent: list[dict[str, Any]]
    ) -> None:
        registry = _WithMain()
        runtime.registry = registry
        await registry.main.nodes.receive_heartbeat("rpi", {"agents": ["camera"]})

        for _ in range(3):
            await mqtt.handle_message("nodes/rpi/heartbeat", "{}")

        assert len(_node_frames(sent)) == 1


class TestAResetSilencesStateUpdates:
    async def test_no_patch_is_sent_while_a_hard_reset_runs(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # A snapshot mid-reset describes a torn-down system; the reset frame
        # carries the survivors itself when it is done.
        runtime.hard_resetting = True

        await mqtt.handle_message("agents/a1/metrics", json.dumps({"cpu": 1.5}))

        assert not _patches(sent)

    async def test_the_raw_relay_still_happens(self, sent: list[dict[str, Any]]) -> None:
        # Only the *state* is suppressed — activity is still observable.
        runtime.hard_resetting = True

        await mqtt.handle_message("agents/a1/metrics", json.dumps({"cpu": 1.5}))

        assert [f for f in sent if f.get("type") == "server_event"]


class TestWhatGoesInTheActivityLog:
    async def test_a_heartbeat_updates_state_without_logging_an_event(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # Heartbeats arrive per agent on a timer; logging each would bury
        # everything else in the feed.
        await mqtt.handle_message(
            "agents/a1/heartbeat", json.dumps({"name": "worker", "state": "running"})
        )

        assert _patches(sent)[0]["event"] is None

    async def test_anything_else_is_logged(self, sent: list[dict[str, Any]]) -> None:
        await mqtt.handle_message("agents/a1/alert", json.dumps({"message": "hot"}))

        assert _patches(sent)[0]["event"] is not None


class TestTotalsAreQueriedOnlyWhenTheyChange:
    """Totals are the only part of a snapshot that hits the database."""

    async def test_a_chat_frame_carries_fresh_totals(self, sent: list[dict[str, Any]]) -> None:
        # An agent spending money is followed by a chat frame, and it is what
        # someone watching the cost is waiting to see. Taking this away is what
        # made the dashboard's cost figure stop updating live.
        with patch.object(mqtt.events, "snapshot", return_value={}) as snapshot:
            await mqtt.handle_message("agents/a1/chat", json.dumps({"content": "done"}))

        assert snapshot.call_args.kwargs["include_totals"] is True

    @pytest.mark.parametrize("metric", ["heartbeat", "metrics", "status", "alert"])
    async def test_timer_driven_frames_do_not(
        self, sent: list[dict[str, Any]], metric: str
    ) -> None:
        # heartbeat and metrics both fire on the heartbeat loop, so querying
        # there would scale database work with agent count — which is exactly
        # what taking totals off this path was for.
        with patch.object(mqtt.events, "snapshot", return_value={}) as snapshot:
            await mqtt.handle_message(f"agents/a1/{metric}", json.dumps({"name": "worker"}))

        assert snapshot.call_args.kwargs["include_totals"] is False


class TestAnAgentsMessageToTheUserIsPersisted:
    async def test_it_is_written_once_as_an_assistant_turn(
        self, sent: list[dict[str, Any]], db: _Db
    ) -> None:
        # The browser already renders it from the relay above, so this row exists
        # only so the turn survives a reload.
        await mqtt.handle_message(
            "agents/a1/chat", json.dumps({"from": "worker", "content": "all done"})
        )

        assert len(db.rows) == 1
        assert db.rows[0]["role"] == "assistant"
        assert db.rows[0]["agent_name"] == "worker"

    async def test_credentials_are_redacted_first(
        self, sent: list[dict[str, Any]], db: _Db
    ) -> None:
        # An agent can quote back something the user typed, and this row
        # outlives the turn.
        await mqtt.handle_message(
            "agents/a1/chat", json.dumps({"from": "worker", "content": "using password=hunter2"})
        )

        assert "hunter2" not in db.rows[0]["content"]

    async def test_an_empty_message_is_not_stored(
        self, sent: list[dict[str, Any]], db: _Db
    ) -> None:
        await mqtt.handle_message("agents/a1/chat", json.dumps({"from": "worker", "content": ""}))

        assert not db.rows

    async def test_a_failing_database_does_not_break_the_broker_loop(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # This runs inside the message loop; raising here would stop ingest for
        # every agent.
        runtime.db = None

        await mqtt.handle_message("agents/a1/chat", json.dumps({"from": "worker", "content": "hi"}))

        assert _patches(sent), "the frame still went out"


class TestTheConnectionBadge:
    async def test_a_change_is_announced(self, sent: list[dict[str, Any]]) -> None:
        runtime.mqtt_connected = False

        await mqtt.set_mqtt_status(True)

        assert sent[-1] == {"type": "mqtt_status", "connected": True}
        assert runtime.mqtt_connected is True

    async def test_no_change_says_nothing(self, sent: list[dict[str, Any]]) -> None:
        # The probe re-asserts the same state constantly; announcing each one
        # would be pure noise on every open dashboard.
        runtime.mqtt_connected = True

        await mqtt.set_mqtt_status(True)

        assert not sent
