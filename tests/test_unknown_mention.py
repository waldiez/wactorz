"""A mention that names nothing must not strand the turn that sent it.

The dashboard only promotes a leading ``@name`` to a routing target when it
names an agent it knows, so a typo leaves the turn addressed to the thread the
user has open and sends the text on verbatim. The server reads the mention back
out of that text, so the two disagree about whose turn it is — and the browser
ends a turn only on a frame from the agent it is waiting on, which is how a
misspelled mention left the thinking dots and a disabled composer on screen
until a timeout let go of them.

So a turn is attributed to the mention only when there is something behind it,
and the answer that says there is not ends the turn like any other ending.
"""

import asyncio
import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.addresses import Address
from wactorz.core.registry import ActorRegistry
from wactorz.web import chat, runtime


class _Registry:
    """Answers for the agents a test says are running locally."""

    def __init__(self, *running: Any) -> None:
        self._running = {a.name: a for a in running}
        for agent in running:
            if isinstance(agent, MainActor):
                # Main finds the agents beside it through the registry it is in.
                agent._registry = cast(ActorRegistry, self)  # finds by name, as the real one does

    def all_actors(self) -> list[Any]:
        return list(self._running.values())

    def find_by_name(self, name: str) -> Any | None:
        return self._running.get(name)


def _agent(name: str) -> Any:
    """Something registered under a name; only the name is ever read."""
    agent = MainActor.__new__(MainActor)
    agent.name = name
    return agent


def _main_with_nodes(
    nodes: dict[str, dict[str, Any]],
    *,
    placed: dict[str, str] | None = None,
    started: float | None = None,
) -> Any:
    """A main actor whose heartbeat table says what runs where.

    `_known_nodes` reads through `self.nodes`, so the table is put where the
    property looks for it rather than on the actor. ``placed`` is what its
    spawn registry places on which node, and ``started`` when it started:
    long ago unless a test says otherwise.
    """
    main = _agent("main")
    registry = {name: {"name": name, "node": node} for name, node in (placed or {}).items()}

    def speaks_for(node: str, name: str) -> bool:
        # As main's own: a node speaks for what the registry places on it, or
        # for what it places nowhere.
        return registry.get(name, {}).get("node", node) == node

    main.nodes = SimpleNamespace(
        known=nodes,
        speaks_for=speaks_for,
        # No manifest has arrived: tasks go by name.
        takes_tasks_by_node=lambda _node: None,
        nodes_for=lambda name: [
            node
            for node, info in nodes.items()
            if name in info["agents"] and speaks_for(node, name)
        ],
    )
    main.spawns = SimpleNamespace(_get_spawn_registry=lambda: SpawnRegistry.from_names(registry))
    main.metrics = SimpleNamespace(start_time=time.time() - 3600 if started is None else started)
    return main


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    saved = runtime.registry
    yield
    runtime.registry = saved


class TestWhoATurnBelongsTo:
    def test_a_mention_of_a_running_agent_names_that_agent(self) -> None:
        runtime.registry = _Registry(_agent("weather-agent"))

        assert chat.turn_attribution("@weather-agent Athens", "main") == "weather-agent"

    def test_an_unknown_mention_belongs_to_the_thread_it_was_sent_from(self) -> None:
        # Where the user is looking. Filed under the mention it would be filed
        # under an agent that does not exist, which no thread ever shows.
        runtime.registry = _Registry(_agent("weather-agent"))

        assert chat.turn_attribution("@weather Athens", "catalog-agent") == "catalog-agent"

    def test_an_unknown_mention_from_nowhere_in_particular_falls_back_to_main(self) -> None:
        runtime.registry = _Registry()

        assert chat.turn_attribution("@weather Athens", "") == "main"

    def test_an_unmentioned_message_belongs_to_main(self) -> None:
        runtime.registry = _Registry(_main_with_nodes({}))

        assert chat.turn_attribution("what is the weather", "catalog-agent") == "main"

    def test_a_slash_command_belongs_to_main_whatever_thread_it_was_typed_in(self) -> None:
        # It is answered by main before any agent sees it.
        runtime.registry = _Registry(_main_with_nodes({}))

        assert chat.turn_attribution("/agents", "catalog-agent") == "main"

    def test_an_agent_on_a_node_heard_from_recently_is_routable(self) -> None:
        nodes = {"rpi": {"last_seen": time.time(), "agents": ["collector"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))

        assert chat.turn_attribution("@collector how hot", "main") == "collector"

    def test_an_agent_on_a_node_gone_quiet_is_not(self) -> None:
        # The dashboard may still offer the thread; this process will not route
        # there, so its refusal belongs in the thread the user sent from --
        # which, for an agent the dashboard lists, is that same agent.
        stale = time.time() - chat.NODE_FRESH_SECONDS - 1
        nodes = {"rpi": {"last_seen": stale, "agents": ["collector"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))

        assert chat.turn_attribution("@collector how hot", "collector") == "collector"


class TestJustAfterMainStarts:
    """No node has reported yet, though the nodes kept running across the restart."""

    def test_an_agent_the_registry_places_on_a_node_is_routed_there(self) -> None:
        main = _main_with_nodes({}, placed={"counter": "edge"}, started=time.time())
        runtime.registry = _Registry(main)

        assert chat.resolution_of("counter").found == Address("counter", "edge")
        assert chat.turn_attribution("@counter five", "main") == "counter"

    def test_once_the_node_reports_its_heartbeat_decides(self) -> None:
        # It reported without the agent: the registry's word no longer counts.
        nodes = {"edge": {"last_seen": time.time(), "agents": ["asker"]}}
        main = _main_with_nodes(nodes, placed={"counter": "edge"}, started=time.time())
        runtime.registry = _Registry(main)

        assert chat.resolution_of("counter").found is None

    def test_after_the_window_a_node_that_never_reported_has_nothing(self) -> None:
        # Routed there for ever, every message to a node that is gone would
        # wait out the whole reply timeout.
        long_ago = time.time() - chat.NODE_FRESH_SECONDS - 1
        main = _main_with_nodes({}, placed={"counter": "edge"}, started=long_ago)
        runtime.registry = _Registry(main)

        assert chat.resolution_of("counter").found is None

    def test_an_agent_placed_on_main_is_not_taken_for_a_remote_one(self) -> None:
        main = _main_with_nodes({}, placed={"greeter": ""}, started=time.time())
        runtime.registry = _Registry(main)

        assert chat.resolution_of("greeter").found is None
        assert chat.resolution_of("nobody").found is None


class TestTheAnswerThatFindsNothing:
    async def test_it_says_so_and_ends_the_turn(self) -> None:
        runtime.registry = _Registry()
        replies: list[str] = []
        ends = {"n": 0}

        async def _reply(message: str) -> None:
            replies.append(message)

        async def _end() -> None:
            ends["n"] += 1

        await chat.route_chat("@weather Athens", _reply, stream_end_fn=_end)

        assert "not found" in replies[0]
        assert ends["n"] == 1, "a turn nothing answers still has to end"


class TestANameTwoNodesShare:
    """Two nodes report an agent of one name: a bare mention is answered, never guessed."""

    @staticmethod
    def _two_counters() -> None:
        now = time.time()
        nodes = {
            "pi": {"last_seen": now, "agents": ["counter"]},
            "pc": {"last_seen": now, "agents": ["counter"]},
        }
        runtime.registry = _Registry(_main_with_nodes(nodes))

    async def test_the_person_is_told_the_addresses_and_nothing_is_sent(self) -> None:
        self._two_counters()
        replies: list[str] = []
        ends = {"n": 0}

        async def _reply(message: str) -> None:
            replies.append(message)

        async def _end() -> None:
            ends["n"] += 1

        destination = chat.destination_of("@counter five")
        await chat.route_chat("@counter five", _reply, stream_end_fn=_end)

        assert destination.kind == "unrouted"
        assert "`@counter@pi` or `@counter@pc`" in replies[0]
        assert ends["n"] == 1

    async def test_a_sender_that_offers_choices_is_given_the_addresses_to_pick_from(self) -> None:
        self._two_counters()
        replies: list[str] = []
        offered: list[tuple[str, tuple[str, ...]]] = []

        async def _reply(message: str) -> None:
            replies.append(message)

        async def _offer(message: str, addresses: tuple[str, ...]) -> None:
            offered.append((message, addresses))

        await chat.route_chat("@counter five", _reply, choices_fn=_offer)

        assert replies == []
        assert offered == [
            (chat.destination_of("@counter five").refusal, ("counter@pi", "counter@pc"))
        ]

    async def test_a_mention_of_nobody_is_not_offered_choices(self) -> None:
        self._two_counters()
        replies: list[str] = []
        offered: list[tuple[str, ...]] = []

        async def _reply(message: str) -> None:
            replies.append(message)

        async def _offer(_message: str, addresses: tuple[str, ...]) -> None:
            offered.append(addresses)

        await chat.route_chat("@weather five", _reply, choices_fn=_offer)

        assert offered == []
        assert "not found" in replies[0]

    def test_the_answer_stays_in_the_thread_it_was_asked_from(self) -> None:
        self._two_counters()

        assert chat.turn_attribution("@counter five", "main") == "main"

    def test_the_full_address_of_one_of_them_is_refused_too(self) -> None:
        # The task would go on the name's topic, which both nodes run, so it is
        # answered where the person is, like any mention that reaches nobody.
        self._two_counters()

        destination = chat.destination_of("@counter@pc five")

        assert destination.kind == "unrouted"
        assert "takes a task only by name" in destination.refusal
        assert chat.turn_attribution("@counter@pc five", "main") == "main"

    def test_the_full_address_of_the_only_one_goes_to_that_node(self) -> None:
        nodes = {"pc": {"last_seen": time.time(), "agents": ["counter"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))

        destination = chat.destination_of("@counter@pc five")

        assert (destination.kind, destination.name, destination.text) == (
            "remote",
            "counter",
            "five",
        )
        assert destination.remote_node == "pc"
        assert chat.turn_attribution("@counter@pc five", "main") == "counter"

    def test_a_node_named_in_the_mention_decides_whose_turn_it_is(self) -> None:
        # Typed in the thread of a `counter` on another node, the turn is the
        # one on pc's, whatever the page said its thread was.
        nodes = {"pc": {"last_seen": time.time(), "agents": ["counter"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))
        runtime.state["agents"]["pc-counter-id"] = {"name": "counter", "node": "pc"}
        try:
            owner = chat.turn_owner("@counter@pc five", "counter", PI_COUNTER_ID)
        finally:
            runtime.state["agents"].pop("pc-counter-id")

        assert owner == chat.TurnOwner("counter", "pc-counter-id")

    def test_a_bare_mention_of_the_threads_own_agent_keeps_the_pages_id(self) -> None:
        nodes = {"pc": {"last_seen": time.time(), "agents": ["counter"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))

        owner = chat.turn_owner("@counter five", "counter", PI_COUNTER_ID)

        assert owner == chat.TurnOwner("counter", PI_COUNTER_ID)


#: The id the page keys a thread for a `counter` by.
PI_COUNTER_ID = "6a0f2c4e-0b6a-4c2e-9a83-5f6f0b1d2c3e"


class TestThePostedMessage:
    """``POST /api/chat`` names its agent the way a mention does."""

    @staticmethod
    async def _post(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> tuple[Any, list[str]]:
        routed: list[str] = []
        tasks: list[asyncio.Task[Any]] = []

        async def _route(content: str, _reply: Any) -> None:
            routed.append(content)

        monkeypatch.setattr(chat, "route_chat", _route)
        monkeypatch.setattr(chat, "track_chat_task", tasks.append)
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        response = await chat.rest_chat_handler(request)
        await asyncio.gather(*tasks)
        return response, routed

    async def test_a_name_two_nodes_report_is_refused_with_the_addresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        TestANameTwoNodesShare._two_counters()

        response, routed = await self._post(
            monkeypatch, {"message": "five", "agent_name": "counter"}
        )

        assert response.status == 409
        assert "`counter@pi` or `counter@pc`" in json.loads(response.body)["error"]
        assert routed == []

    async def test_one_of_two_nodes_sharing_the_name_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        TestANameTwoNodesShare._two_counters()

        response, routed = await self._post(
            monkeypatch, {"message": "five", "agent_name": "counter@pc"}
        )

        assert response.status == 409
        assert "takes a task only by name" in json.loads(response.body)["error"]
        assert routed == []

    async def test_an_agent_on_a_node_is_sent_its_whole_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nodes = {"pc": {"last_seen": time.time(), "agents": ["counter"]}}
        runtime.registry = _Registry(_main_with_nodes(nodes))

        response, routed = await self._post(
            monkeypatch, {"message": "five", "agent_name": "counter@pc"}
        )

        assert response.status == 200
        assert routed == ["@counter@pc five"]

    async def test_an_agent_here_is_sent_its_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime.registry = _Registry(_main_with_nodes({}), _agent("weather"))

        _response, routed = await self._post(
            monkeypatch, {"message": "rain?", "agent_name": "weather@main"}
        )

        assert routed == ["@weather rain?"]

    async def test_a_name_nobody_has_is_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime.registry = _Registry(_main_with_nodes({}))

        response, routed = await self._post(monkeypatch, {"message": "hi", "agent_name": "ghost"})

        assert response.status == 404
        assert routed == []
