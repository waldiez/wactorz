"""An agent's address, ``name@node``, and what a name or an address finds.

Two agents on different nodes may one day share a name. Whatever a person or
a program uses to name an agent then either finds one agent, or says which
addresses it could mean: it never picks one.
"""

import time
from typing import Any, cast

import pytest

from wactorz.agents.main.addresses import (
    UNHEARD_GRACE_SECONDS,
    known_addresses,
    registered_name,
    resolve_address,
    task_refusal,
)
from wactorz.agents.main.hosts import AddressHost
from wactorz.agents.main.nodes import NodeManager
from wactorz.config import reserved_node_name_error
from wactorz.core.actor import ActorMetrics
from wactorz.core.addresses import (
    MAIN_NODE,
    Address,
    agent_name_error,
    node_of,
    parse,
    resolve,
)


class TestSpellingAnAddress:
    def test_a_bare_name_names_no_node(self) -> None:
        assert parse("counter") == Address("counter", "")
        assert str(parse("counter")) == "counter"

    def test_name_at_node_names_both(self) -> None:
        assert parse("counter@pi-kitchen") == Address("counter", "pi-kitchen")
        assert str(Address("counter", "pi-kitchen")) == "counter@pi-kitchen"

    @pytest.mark.parametrize("main", ["main", "local", "MAIN", " local "])
    def test_every_name_for_main_is_main(self, main: str) -> None:
        assert node_of(main) == MAIN_NODE
        assert parse(f"counter@{main}") == Address("counter", MAIN_NODE)

    def test_a_separator_with_no_node_after_it_is_the_bare_name(self) -> None:
        assert parse("counter@") == Address("counter", "")


class TestWhatANewNameMayHold:
    def test_the_separator_is_refused_in_a_new_agent_name(self) -> None:
        problem = agent_name_error("counter@pi")

        assert problem is not None
        assert "'@'" in problem

    def test_what_a_topic_level_refuses_is_still_refused(self) -> None:
        assert agent_name_error("count+er") is not None
        assert agent_name_error("") is not None

    def test_an_ordinary_name_is_taken(self) -> None:
        assert agent_name_error("counter") is None

    def test_the_separator_is_refused_in_a_new_node_name(self) -> None:
        problem = reserved_node_name_error("pi@kitchen")

        assert problem is not None
        assert "'@'" in problem
        assert reserved_node_name_error("pi-kitchen") is None


KNOWN = [
    Address("counter", "pi-kitchen"),
    Address("counter", "pi-garage"),
    Address("weather", MAIN_NODE),
]


class TestWhatANameFinds:
    def test_a_name_one_agent_has_finds_it(self) -> None:
        assert resolve("weather", KNOWN).found == Address("weather", MAIN_NODE)

    def test_a_name_two_agents_have_finds_neither(self) -> None:
        resolution = resolve("counter", KNOWN)

        assert resolution.found is None
        assert resolution.ambiguous
        assert resolution.matches == (KNOWN[0], KNOWN[1])

    def test_an_address_finds_the_one_on_its_node(self) -> None:
        assert resolve("counter@pi-garage", KNOWN).found == Address("counter", "pi-garage")

    def test_main_is_found_by_any_of_its_names(self) -> None:
        assert resolve("weather@local", KNOWN).found == Address("weather", MAIN_NODE)

    def test_an_address_on_the_wrong_node_finds_nothing(self) -> None:
        resolution = resolve("weather@pi-garage", KNOWN)

        assert resolution.found is None
        assert not resolution.ambiguous

    def test_a_name_nobody_has_finds_nothing(self) -> None:
        assert resolve("ghost", KNOWN).matches == ()

    def test_an_agent_named_before_the_separator_was_refused_keeps_its_name(self) -> None:
        # Its whole name, not counter on a node called "old".
        known = [*KNOWN, Address("counter@old", MAIN_NODE)]

        assert resolve("counter@old", known).found == Address("counter@old", MAIN_NODE)

    def test_one_place_listed_twice_is_one_agent(self) -> None:
        assert resolve("weather", [*KNOWN, KNOWN[2]]).found == KNOWN[2]


class TestWhatTheSenderIsTold:
    def test_a_person_is_shown_what_to_type(self) -> None:
        said = resolve("counter", KNOWN).refusal(mention=True)

        assert "'counter'" in said
        assert "`@counter@pi-kitchen` or `@counter@pi-garage`" in said
        assert "nothing was done" in said

    def test_a_program_is_given_the_addresses_to_pass(self) -> None:
        known = [*KNOWN, Address("counter", "pc")]

        said = resolve("counter", known).refusal()

        assert "`counter@pi-kitchen`, `counter@pi-garage` or `counter@pc`" in said
        assert "@counter" not in said


class _Registry:
    def __init__(self, *names: str) -> None:
        self._names = set(names)

    def find_by_name(self, name: str) -> object | None:
        return object() if name in self._names else None


class _Main:
    """What main's lookup reads: its own agents, a node table and its spawn registry."""

    def __init__(
        self,
        *,
        here: tuple[str, ...] = (),
        nodes: dict[str, dict[str, Any]] | None = None,
        placed: dict[str, str] | None = None,
        started: float = 0.0,
    ) -> None:
        self._registry = _Registry(*here)
        #: With no host behind it, every node may speak for any agent.
        self.nodes = NodeManager()
        self.nodes.known = nodes or {}
        self.metrics = ActorMetrics(start_time=started)
        self._spawns = {name: {"name": name, "node": node} for name, node in (placed or {}).items()}

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self._spawns


def _host(main: _Main) -> AddressHost:
    return cast(AddressHost, main)  # the attributes the lookup reads, without the actor


def _heard(*agents: str, ago: float = 0.0) -> dict[str, Any]:
    return {"agents": list(agents), "last_seen": time.time() - ago}


class TestWhereMainKnowsAnAgentToBe:
    def test_two_nodes_reporting_one_name_make_it_ambiguous(self) -> None:
        main = _Main(nodes={"pi": _heard("counter"), "pc": _heard("counter")})

        resolution = resolve_address(_host(main), "counter")

        assert resolution.ambiguous
        assert set(resolution.matches) == {Address("counter", "pi"), Address("counter", "pc")}
        assert resolve_address(_host(main), "counter@pc").found == Address("counter", "pc")

    def test_an_agent_here_is_on_main(self) -> None:
        main = _Main(here=("weather",))

        assert resolve_address(_host(main), "weather").found == Address("weather", MAIN_NODE)
        assert resolve_address(_host(main), "weather@main").found == Address("weather", MAIN_NODE)

    def test_a_node_not_heard_from_lately_counts_only_when_asked_to(self) -> None:
        main = _Main(nodes={"pi": _heard("counter", ago=60)})

        assert known_addresses(_host(main), "counter") == [Address("counter", "pi")]
        assert known_addresses(_host(main), "counter", heard_within=30) == []

    def test_just_after_main_starts_the_registry_answers_for_a_node_unheard_from(self) -> None:
        main = _Main(placed={"counter": "pi"}, started=time.time())

        assert known_addresses(_host(main), "counter") == [Address("counter", "pi")]

    def test_after_the_grace_only_heartbeats_count(self) -> None:
        long_ago = time.time() - UNHEARD_GRACE_SECONDS - 1
        main = _Main(placed={"counter": "pi"}, started=long_ago)

        assert known_addresses(_host(main), "counter") == []

    def test_an_agent_placed_on_main_is_not_taken_for_a_remote_one(self) -> None:
        main = _Main(placed={"counter": "main"}, started=time.time())

        assert known_addresses(_host(main), "counter") == []


class TestWhenATaskCanGo:
    """A task for a node's agent goes on the name's topic, which every node running the name hears."""

    def test_to_the_only_node_running_the_name_it_goes(self) -> None:
        main = _Main(here=("counter",), nodes={"pc": _heard("other")})

        assert task_refusal(_host(main), Address("counter", MAIN_NODE)) is None

    def test_to_one_of_two_nodes_running_the_name_it_does_not(self) -> None:
        main = _Main(nodes={"pi": _heard("counter"), "pc": _heard("counter")})

        refusal = task_refusal(_host(main), Address("counter", "pc"))

        assert refusal is not None
        assert "nodes pc and pi" in refusal
        assert "nothing was sent to counter@pc" in refusal

    def test_an_agent_on_main_is_asked_in_process_whoever_else_runs_the_name(self) -> None:
        main = _Main(here=("counter",), nodes={"pi": _heard("counter"), "pc": _heard("counter")})

        assert task_refusal(_host(main), Address("counter", MAIN_NODE)) is None

    def test_a_node_that_alone_runs_the_name_is_sent_it(self) -> None:
        main = _Main(nodes={"pc": _heard("counter")})

        assert task_refusal(_host(main), Address("counter", "pc")) is None


class TestWhatACommandActsOn:
    """Commands act on the spawn registry's entry, which an address has to agree with."""

    def test_a_bare_name_names_its_entry(self) -> None:
        assert registered_name(_host(_Main()), "counter") == ("counter", None)

    def test_an_address_on_main_names_an_agent_main_keeps_no_entry_for(self) -> None:
        # A built-in agent: main runs it, and the registry has no entry for it.
        assert registered_name(_host(_Main()), "catalog@main") == ("catalog", None)

    def test_an_address_where_the_registry_places_it_names_it(self) -> None:
        main = _Main(placed={"counter": "pi"})

        assert registered_name(_host(main), "counter@pi") == ("counter", None)

    def test_an_address_elsewhere_names_nothing(self) -> None:
        main = _Main(placed={"counter": "pi"})

        assert registered_name(_host(main), "counter@pc") == (
            "counter",
            "'counter' is on 'pi', not on 'pc'.",
        )
