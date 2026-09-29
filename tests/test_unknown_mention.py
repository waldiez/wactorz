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

import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.web import chat, runtime


class _Registry:
    """Answers for the agents a test says are running locally."""

    def __init__(self, *running: Any) -> None:
        self._running = {a.name: a for a in running}

    def all_actors(self) -> list[Any]:
        return list(self._running.values())

    def find_by_name(self, name: str) -> Any | None:
        return self._running.get(name)


def _agent(name: str) -> Any:
    """Something registered under a name; only the name is ever read."""
    agent = MainActor.__new__(MainActor)
    agent.name = name
    return agent


def _main_with_nodes(nodes: dict[str, dict[str, Any]]) -> Any:
    """A main actor whose heartbeat table says what runs where.

    `_known_nodes` reads through `self.nodes`, so the table is put where the
    property looks for it rather than on the actor.
    """
    main = _agent("main")
    main.nodes = SimpleNamespace(known=nodes)
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
        runtime.registry = _Registry(_agent("main"))

        assert chat.turn_attribution("what is the weather", "catalog-agent") == "main"

    def test_a_slash_command_belongs_to_main_whatever_thread_it_was_typed_in(self) -> None:
        # It is answered by main before any agent sees it.
        runtime.registry = _Registry(_agent("main"))

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
