"""Main asking the nodes to reset what their agents keep there, and hearing back."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.node_messages import SignedMessage
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.web import api_reset, node_reset


class _Nodes:
    """What main knows of its nodes: which report, and which say they reset."""

    def __init__(self, online: set[str], resets: set[str]) -> None:
        self._online = online
        self._resets = resets

    def is_online(self, node: str) -> bool:
        return node in self._online

    def takes_resets(self, node: str) -> bool:
        return node in self._resets


class _Main:
    """Stands in for main: its node table and where its broker is."""

    _mqtt_broker = "localhost"
    _mqtt_port = 1883

    def __init__(self, online: set[str], resets: set[str]) -> None:
        self.nodes = _Nodes(online, resets)


Answer = Callable[[str, dict[str, Any]], "list[SignedMessage]"]


class _Broker:
    """One connection's worth of broker: records what main sends, delivers what nodes answer."""

    def __init__(self, answer: Answer) -> None:
        self._answer = answer
        self.subscribed: list[str] = []
        self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._inbox: asyncio.Queue[SignedMessage] = asyncio.Queue()

    async def __aenter__(self) -> "_Broker":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kw: Any) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload: str, **kwargs: Any) -> None:
        data = json.loads(payload)
        self.sent.append((topic, data, kwargs))
        for message in self._answer(topic, data):
            self._inbox.put_nowait(message)

    @property
    def messages(self) -> AsyncIterator[SignedMessage]:
        """What the nodes answered, in order, as aiomqtt's ``client.messages`` yields it."""
        return self._messages()

    async def _messages(self) -> AsyncIterator[SignedMessage]:
        while True:
            yield await self._inbox.get()


def _connect(monkeypatch: pytest.MonkeyPatch, answer: Answer) -> _Broker:
    broker = _Broker(answer)
    monkeypatch.setattr(node_reset, "mqtt_client", lambda *_a, **_k: broker)
    return broker


def _done(
    topic: str, data: dict[str, Any], *, request: str | None = None, **over: Any
) -> list[Any]:
    """The answer a node gives to the reset it was sent on `topic`."""
    node = topic.split("/")[1]
    body = {"node": node, "request": request or data["request"], "cleared": ["counter"], **over}
    return [SignedMessage(f"nodes/{node}/reset_done", json.dumps(body))]


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    # Long enough for an answer that is coming, short enough that one that is
    # not does not hold the suite up.
    monkeypatch.setattr(node_reset, "ANSWER_TIMEOUT_S", 0.5)


class TestWhoIsAsked:
    async def test_every_node_with_agents_for_a_reset_of_all_of_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _connect(monkeypatch, _done)
        main = _Main(online={"edge", "pi"}, resets={"edge", "pi"})

        outcome = await node_reset.reset_on_nodes(
            main, "chat", None, {"edge": {"counter"}, "pi": {"lamp"}}
        )

        assert outcome == {"edge": "cleared", "pi": "cleared"}
        assert sorted(t for t, _d, _k in broker.sent) == ["nodes/edge/reset", "nodes/pi/reset"]

    async def test_only_the_node_that_runs_the_agent_for_one_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _connect(monkeypatch, _done)
        main = _Main(online={"edge", "pi"}, resets={"edge", "pi"})

        outcome = await node_reset.reset_on_nodes(
            main, "metrics", "counter", {"edge": {"counter"}, "pi": {"lamp"}}
        )

        assert outcome == {"edge": "cleared"}
        [(topic, data, kwargs)] = broker.sent
        assert topic == "nodes/edge/reset"
        assert (data["scope"], data["agent"]) == ("metrics", "counter")
        # Signed like every other command main sends a node.
        assert kwargs.get("properties") is not None

    async def test_a_node_away_or_on_an_older_release_is_named_and_not_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _connect(monkeypatch, _done)
        main = _Main(online={"edge", "old"}, resets={"edge"})

        outcome = await node_reset.reset_on_nodes(
            main, "chat", None, {"edge": {"a"}, "away": {"b"}, "old": {"c"}}
        )

        # Asked, the first would wait out the timeout and the second never answer.
        assert outcome == {"edge": "cleared", "away": "offline", "old": "older release"}
        assert [t for t, _d, _k in broker.sent] == ["nodes/edge/reset"]

    async def test_nothing_is_sent_when_no_node_is_concerned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _connect(monkeypatch, _done)

        outcome = await node_reset.reset_on_nodes(
            _Main(online={"edge"}, resets={"edge"}), "chat", "greeter", {"edge": {"counter"}}
        )

        assert outcome == {}
        assert broker.sent == []


class TestWhichAnswersCount:
    async def test_one_not_signed_by_the_node_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unsigned(topic: str, data: dict[str, Any]) -> list[Any]:
            [message] = _done(topic, data)
            message.signed = False
            return [message]

        _connect(monkeypatch, unsigned)

        outcome = await node_reset.reset_on_nodes(
            _Main(online={"edge"}, resets={"edge"}), "chat", None, {"edge": {"counter"}}
        )

        # Anyone can publish on the topic; only the node can sign for it.
        assert outcome == {"edge": "no answer"}

    async def test_one_quoting_another_request_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _connect(monkeypatch, lambda topic, data: _done(topic, data, request="an-earlier-one"))

        outcome = await node_reset.reset_on_nodes(
            _Main(online={"edge"}, resets={"edge"}), "chat", None, {"edge": {"counter"}}
        )

        assert outcome == {"edge": "no answer"}

    async def test_a_node_that_answers_and_one_that_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _connect(monkeypatch, lambda topic, data: _done(topic, data) if "edge" in topic else [])

        outcome = await node_reset.reset_on_nodes(
            _Main(online={"edge", "pi"}, resets={"edge", "pi"}),
            "chat",
            None,
            {"edge": {"counter"}, "pi": {"lamp"}},
        )

        assert outcome == {"edge": "cleared", "pi": "no answer"}

    async def test_no_broker_is_no_answer_rather_than_a_failed_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Refused:
            async def __aenter__(self) -> None:
                raise OSError("connection refused")

            async def __aexit__(self, *_exc: object) -> None:
                return None

        monkeypatch.setattr(node_reset, "mqtt_client", lambda *_a, **_k: _Refused())

        outcome = await node_reset.reset_on_nodes(
            _Main(online={"edge"}, resets={"edge"}), "chat", None, {"edge": {"counter"}}
        )

        # Main's own records are cleared already; this only says the node was not.
        assert outcome == {"edge": "no answer"}


class _SpawnedOnEdge:
    """Main, as far as the reset route reads it: one agent placed on a node, and no node heard from."""

    _known_nodes: ClassVar[dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _get_spawn_registry() -> SpawnRegistry:
        return SpawnRegistry.from_names({"counter": {"name": "counter", "node": "edge"}})


class TestTheResetRoute:
    """`POST /api/reset` asks the nodes for the scopes that reach into an agent, and says what they did."""

    @staticmethod
    async def _post(
        monkeypatch: pytest.MonkeyPatch,
        body: dict[str, Any],
        status: int = 200,
        *,
        cleared: list[dict[str, Any]] | None = None,
        frames: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[tuple[str, str | None, dict[str, set[str]]]]]:
        asked: list[tuple[str, str | None, dict[str, set[str]]]] = []

        async def reset_on_nodes(
            _main: Any, scope: str, agent: str | None, on: dict[str, set[str]]
        ) -> dict[str, str]:
            asked.append((scope, agent, on))
            return {"edge": "cleared"}

        monkeypatch.setattr(node_reset, "reset_on_nodes", reset_on_nodes)
        monkeypatch.setattr(api_reset, "find_main_actor", lambda _registry: _SpawnedOnEdge())
        monkeypatch.setattr(api_reset.runtime, "registry", None)

        async def broadcast(frame: dict[str, Any]) -> None:
            if frames is not None:
                frames.append(frame)

        def reset_chat(*_args: Any, **kwargs: Any) -> None:
            if cleared is not None:
                cleared.append(kwargs)

        monkeypatch.setattr(api_reset.ws, "broadcast", broadcast)
        monkeypatch.setattr("wactorz.reset.reset_chat", reset_chat)
        for name in (
            "reset_agent_state",
            "reset_metrics",
            "reset_spawns",
            "reset_logs",
        ):
            monkeypatch.setattr(f"wactorz.reset.{name}", lambda *_a, **_k: None)
        monkeypatch.setattr(api_reset.lifecycle, "purge_spawn_reconcile", AsyncMock())
        request = MagicMock()
        request.json = AsyncMock(return_value=body)

        response = await api_reset.reset_handler(request)

        assert response.status == status
        return json.loads(response.body), asked  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize("scope", ["chat", "state", "metrics"])
    async def test_a_scope_an_agent_keeps_a_copy_of_reaches_its_node(
        self, monkeypatch: pytest.MonkeyPatch, scope: str
    ) -> None:
        answer, asked = await self._post(monkeypatch, {"scope": scope, "agent": "counter"})

        assert [(s, a) for s, a, _on in asked] == [(scope, "counter")]
        assert asked[0][2].get("edge") == {"counter"}
        assert answer["nodes"] == {"edge": "cleared"}

    async def test_an_address_resets_the_agent_it_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answer, asked = await self._post(monkeypatch, {"scope": "chat", "agent": "counter@edge"})

        assert [(s, a) for s, a, _on in asked] == [("chat", "counter")]
        assert answer["agent"] == "counter"

    async def test_a_name_taken_before_the_separator_was_refused_is_reset_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(api_reset.runtime.state, "nodes", {"pi": {"agents": ["counter@old"]}})

        answer, asked = await self._post(monkeypatch, {"scope": "chat", "agent": "counter@old"})

        assert [(s, a) for s, a, _on in asked] == [("chat", "counter@old")]
        assert answer["agent"] == "counter@old"

    async def test_an_address_on_a_node_without_the_name_resets_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not `counter` wherever it is instead.
        answer, asked = await self._post(
            monkeypatch, {"scope": "chat", "agent": "counter@wrong"}, status=404
        )

        assert answer["error"] == "No agent 'counter' is on node 'wrong'."
        assert asked == []

    async def test_a_name_on_two_nodes_resets_neither(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(api_reset.runtime.state, "nodes", {"pi": {"agents": ["counter"]}})

        answer, asked = await self._post(
            monkeypatch, {"scope": "chat", "agent": "counter"}, status=409
        )

        assert "`counter@edge` or `counter@pi`" in answer["error"]
        assert asked == []

    async def test_a_node_only_main_has_heard_from_yet_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The dashboard is shown a heartbeat after main takes it, so main's
        # table can be a heartbeat ahead.
        monkeypatch.setattr(_SpawnedOnEdge, "_known_nodes", {"pi": {"agents": ["counter"]}})

        answer, asked = await self._post(
            monkeypatch, {"scope": "chat", "agent": "counter"}, status=409
        )

        assert "`counter@edge` or `counter@pi`" in answer["error"]
        assert asked == []

    @staticmethod
    def _shared(monkeypatch: pytest.MonkeyPatch) -> None:
        """``counter`` on edge, as placed, and on pi, known by its heartbeat."""
        monkeypatch.setitem(api_reset.runtime.state, "nodes", {"pi": {"agents": ["counter"]}})
        monkeypatch.setitem(
            api_reset.runtime.state,
            "agents",
            {"id-on-pi": {"name": "counter", "node": "pi"}},
        )

    async def test_one_of_two_on_nodes_asks_its_node_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._shared(monkeypatch)

        answer, asked = await self._post(monkeypatch, {"scope": "chat", "agent": "counter@pi"})

        assert asked == [("chat", "counter", {"pi": {"counter"}})]
        assert answer["agent"] == "counter@pi"

    async def test_one_of_two_is_cleared_by_its_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._shared(monkeypatch)
        cleared: list[dict[str, Any]] = []
        frames: list[dict[str, Any]] = []

        await self._post(
            monkeypatch,
            {"scope": "chat", "agent": "counter@pi"},
            cleared=cleared,
            frames=frames,
        )

        assert cleared == [{"actor_id": "id-on-pi", "on_main": False}]
        assert frames[0]["agent_id"] == "id-on-pi"

    async def test_an_address_with_no_id_to_go_by_resets_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # By name instead, it would take the other agent's history with it.
        monkeypatch.setitem(api_reset.runtime.state, "nodes", {"pi": {"agents": ["counter"]}})
        monkeypatch.setitem(api_reset.runtime.state, "agents", {})

        answer, asked = await self._post(
            monkeypatch, {"scope": "chat", "agent": "counter@pi"}, status=409
        )

        assert "cannot be told" in answer["error"]
        assert asked == []

    @pytest.mark.parametrize("scope", ["spawns", "logs"])
    async def test_one_that_concerns_main_alone_asks_no_node(
        self, monkeypatch: pytest.MonkeyPatch, scope: str
    ) -> None:
        answer, asked = await self._post(monkeypatch, {"scope": scope})

        assert asked == []
        assert answer["nodes"] == {}


class _Supervisor:
    """Records the agents it was asked to start again, and runs what goes between."""

    def __init__(self, supervised: set[str]) -> None:
        self._specs = dict.fromkeys(supervised)
        self.restarted: list[str] = []

    async def restart_afresh(self, name: str, between: Callable[[], object]) -> bool:
        between()
        self.restarted.append(name)
        return True


class _Registry:
    def __init__(self, actors: list[Any], supervisor: _Supervisor) -> None:
        self._actors = actors
        self._supervisor_ref = supervisor

    def all_actors(self) -> list[Any]:
        return self._actors

    def find_by_name(self, name: str) -> Any | None:
        return next((actor for actor in self._actors if actor.name == name), None)


class TestAStateResetHere:
    """An agent forgets its state only by starting again, so a state reset restarts it."""

    @staticmethod
    def _actor(name: str, *, protected: bool = False) -> Any:
        actor = MagicMock()
        actor.name = name
        actor.protected = protected
        actor._persistent_state = {}
        return actor

    async def _reset(
        self, monkeypatch: pytest.MonkeyPatch, agent: str | None
    ) -> tuple[_Supervisor, list[str]]:
        supervisor = _Supervisor({"main", "counter", "greeter"})
        actors = [
            self._actor("main", protected=True),
            self._actor("counter"),
            self._actor("greeter"),
            self._actor("loose"),
        ]
        cleared: list[str] = []
        monkeypatch.setattr(api_reset.runtime, "registry", _Registry(actors, supervisor))
        monkeypatch.setattr(api_reset, "find_main_actor", lambda _registry: None)
        monkeypatch.setattr(api_reset.ws, "broadcast", AsyncMock())
        monkeypatch.setattr("wactorz.reset.reset_agent_state", cleared.append)
        monkeypatch.setattr("wactorz.reset._reset_all_pickles", lambda: cleared.append("*"))
        request = MagicMock()
        request.json = AsyncMock(return_value={"scope": "state", "agent": agent})

        response = await api_reset.reset_handler(request)

        assert response.status == 200
        return supervisor, cleared

    async def test_one_agent_is_started_again_with_its_file_gone_meanwhile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor, cleared = await self._reset(monkeypatch, "counter")

        assert supervisor.restarted == ["counter"]
        assert cleared[0] == "counter"

    async def test_all_of_them_but_the_systems_own_and_those_nothing_supervises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Main runs this very request, and nothing would bring back an agent no
        # supervisor holds; those keep running with their copies cleared.
        supervisor, cleared = await self._reset(monkeypatch, None)

        assert sorted(supervisor.restarted) == ["counter", "greeter"]
        assert "*" in cleared


class _Ledger:
    """A store that records what the lifetime ledger was saved as."""

    def __init__(self) -> None:
        self.saved: list[dict[str, float]] = []

    def kv_set(self, _owner: str, _key: str, value: dict[str, float]) -> None:
        self.saved.append(dict(value))

    def kv_get(self, _owner: str, _key: str) -> None:
        return None


class TestAMetricsResetOfANodeAgent:
    async def test_its_banked_cost_goes_from_memory_and_from_the_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Banked under its own id, which no actor in this process has: looked up
        # only here, the old high-water would go on pinning the total.
        ledger = _Ledger()
        monkeypatch.setattr(api_reset.runtime, "db", ledger)
        monkeypatch.setattr(
            api_reset.runtime,
            "state",
            {
                **api_reset.runtime.state,
                "agents": {"id-counter": {"name": "counter", "node": "edge"}},
            },
        )
        monkeypatch.setattr(api_reset.cost, "lifetime_cost", {"id-counter": 5.0, "id-other": 1.0})
        monkeypatch.setattr(api_reset.cost, "lifetime_loaded", True)

        await TestTheResetRoute._post(monkeypatch, {"scope": "metrics", "agent": "counter"})

        assert api_reset.cost.lifetime_cost == {"id-other": 1.0}
        assert ledger.saved[-1] == {"id-other": 1.0}
