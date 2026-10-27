"""Where a task for an agent goes: its node's own topic, or its name for an older node.

``agents/at/<node>/<name>/task`` reaches that node alone, so two agents of one
name on two nodes are each reached by their address. A node from before that
hears only ``agents/by-name/<name>/task``, which every node hears.
"""

import json
from typing import Any, cast

from wactorz.agents.dynamic.messaging import Route, route_from_node
from wactorz.agents.main.addresses import task_refusal, task_topic_for
from wactorz.agents.main.hosts import AddressHost
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import ActorMetrics
from wactorz.core.addresses import Address
from wactorz.core.agent_directory import AgentDirectory
from wactorz.core.topics import (
    is_task_route,
    node_task_subscription,
    reply_topic,
    task_by_name_topic,
    task_target,
    task_topic,
)
from wactorz.core.turns import is_task_topic
from wactorz.node.hosted import NodeHosted


class TestSpellingTheTopics:
    def test_a_task_by_node_and_by_name(self) -> None:
        assert task_topic("pi", "counter") == "agents/at/pi/counter/task"
        assert task_by_name_topic("counter") == "agents/by-name/counter/task"
        assert node_task_subscription("pi") == "agents/at/pi/+/task"
        assert reply_topic("pi", "asker", "k1") == "agents/at/pi/asker/reply/k1"

    def test_the_agent_a_task_is_for_is_read_from_either(self) -> None:
        assert task_target("agents/at/pi/counter/task") == "counter"
        assert task_target("agents/by-name/counter/task") == "counter"

    def test_what_is_no_task_names_no_agent(self) -> None:
        assert task_target("agents/at/pi/asker/reply/k1") is None
        assert task_target("agents/c0ffee/status") is None
        assert task_target("agents/at/counter/task") is None
        assert task_target("nodes/pi/task") is None

    def test_a_task_of_either_kind_carries_its_turn(self) -> None:
        assert is_task_topic("agents/at/pi/counter/task")
        assert is_task_topic("agents/by-name/counter/task")
        assert not is_task_topic("agents/at/pi/asker/reply/k1")

    def test_replies_are_under_the_task_routes_too(self) -> None:
        assert is_task_route("agents/at/pi/asker/reply/k1")
        assert is_task_route("agents/by-name/asker/reply/k1")
        assert not is_task_route("agents/c0ffee/status")


class _Registry:
    def find_by_name(self, _name: str) -> object | None:
        return None


class _Main:
    """What main's choice of topic reads: the node table with its manifests."""

    _registry = _Registry()

    def __init__(self, nodes: dict[str, list[str]], manifests: dict[str, dict[str, Any]]) -> None:
        self.nodes = NodeManager()
        self.nodes.known = {node: {"agents": agents} for node, agents in nodes.items()}
        self.nodes.node_manifests = manifests
        self.metrics = ActorMetrics(start_time=0.0)

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return {}


def _host(main: _Main) -> AddressHost:
    return cast(AddressHost, main)  # the attributes the choice reads, without the actor


BY_NODE = {"tasks_by_node": True}


class TestWhichTopicMainSendsOn:
    def test_a_node_that_says_so_is_sent_on_its_own_topic(self) -> None:
        main = _Main({"pi": ["counter"]}, {"pi": BY_NODE})

        topic = task_topic_for(_host(main), Address("counter", "pi"))

        assert topic == "agents/at/pi/counter/task"

    def test_an_older_node_is_sent_by_name(self) -> None:
        main = _Main({"pi": ["counter"]}, {"pi": {"actor_ids": True}})

        assert task_topic_for(_host(main), Address("counter", "pi")) == (
            "agents/by-name/counter/task"
        )

    def test_a_node_whose_manifest_has_not_come_is_sent_by_name(self) -> None:
        main = _Main({"pi": ["counter"]}, {})

        assert task_topic_for(_host(main), Address("counter", "pi")) == (
            "agents/by-name/counter/task"
        )

    def test_one_of_two_nodes_sharing_a_name_is_reached_alone_on_its_own_topic(self) -> None:
        main = _Main({"pi": ["counter"], "pc": ["counter"]}, {"pc": BY_NODE})

        assert task_refusal(_host(main), Address("counter", "pc")) is None

    def test_an_older_one_of_them_is_not_sent_it(self) -> None:
        # By name, the newer node would run it as well: it hears by name too.
        main = _Main({"pi": ["counter"], "pc": ["counter"]}, {"pc": BY_NODE})

        refusal = task_refusal(_host(main), Address("counter", "pi"))

        assert refusal is not None
        assert "node 'pi' takes a task only by name" in refusal
        assert "Deploy 'pi' again" in refusal


def _manifest(name: str, node: str | None, *, by_node: bool = True) -> bytes:
    body: dict[str, Any] = {"name": name, "actor_id": f"id-{name}-{node}"}
    if node is not None:
        body["node"] = node
    if by_node:
        body["tasks_by_node"] = True
    return json.dumps(body).encode()


def _heard(*manifests: tuple[str, str | None, bool]) -> AgentDirectory:
    directory = AgentDirectory()
    for name, node, by_node in manifests:
        directory.take(f"agents/id-{name}-{node}/manifest", _manifest(name, node, by_node=by_node))
    return directory


class TestWhatANodeHearsOfTheOthers:
    def test_an_agent_is_placed_on_the_node_its_manifest_names(self) -> None:
        directory = _heard(("counter", "pc", True))

        assert directory.resolve("counter").found == Address("counter", "pc")
        assert directory.takes_tasks_by_node(Address("counter", "pc"))

    def test_one_that_names_no_node_has_no_known_place(self) -> None:
        # Main's own agents send none, and nor does one on a node from before
        # nodes said so: it is neither taken for main's nor counted as a node's.
        directory = _heard(("weather", None, False))

        assert directory.resolve("weather").matches == ()
        assert directory.unplaced("weather")
        assert directory.nodes_running("weather") == set()

    def test_a_withdrawn_manifest_takes_the_agent_away(self) -> None:
        directory = _heard(("counter", "pc", True))

        directory.take("agents/id-counter-pc/manifest", b"")

        assert directory.resolve("counter").matches == ()

    def test_what_is_no_manifest_is_ignored(self) -> None:
        directory = AgentDirectory()

        directory.take("agents/x/status", b'{"name": "x"}')
        directory.take("agents/x/manifest", b"not json")
        directory.take("agents/x/manifest", b'["a list"]')
        directory.take("agents/x/manifest", b'{"no": "name"}')

        assert directory.resolve("x").matches == ()

    def test_the_nodes_running_a_name_are_the_known_ones(self) -> None:
        directory = _heard(
            ("counter", "pc", True), ("counter", "pi", False), ("counter", None, False)
        )

        assert directory.nodes_running("counter") == {"pc", "pi"}


class TestWhereATaskFromANodeGoes:
    def test_to_a_node_that_takes_them_on_its_own_topic(self) -> None:
        directory = _heard(("counter", "pc", True), ("counter", "pi", True))

        route = route_from_node(directory, "counter@pc", "edge")

        assert route == Route(Address("counter", "pc"), "agents/at/pc/counter/task")

    def test_a_name_two_nodes_share_is_refused_with_the_addresses(self) -> None:
        directory = _heard(("counter", "pc", True), ("counter", "pi", True))

        route = route_from_node(directory, "counter", "edge")

        assert isinstance(route, str)
        assert "`counter@pc` or `counter@pi`" in route

    def test_to_an_older_node_alone_by_name(self) -> None:
        directory = _heard(("counter", "pi", False))

        assert route_from_node(directory, "counter", "edge") == Route(
            Address("counter", "pi"), "agents/by-name/counter/task"
        )

    def test_to_an_older_node_sharing_the_name_it_is_not_sent(self) -> None:
        directory = _heard(("counter", "pi", False), ("counter", "pc", True))

        route = route_from_node(directory, "counter@pi", "edge")

        assert isinstance(route, str)
        assert "also run on pc" in route

    def test_one_on_this_node_is_asked_in_process(self) -> None:
        directory = _heard(("counter", "edge", True))

        assert route_from_node(directory, "counter", "edge") == Route(
            Address("counter", "edge"), ""
        )

    def test_an_agent_on_an_older_node_is_sent_by_name(self) -> None:
        # What a library agent on a node from before this release leaves
        # retained: a manifest with neither `node` nor `tasks_by_node`.
        directory = AgentDirectory()
        directory.take(
            "agents/legacy-worker/manifest",
            json.dumps(
                {"name": "worker", "actor_id": "legacy-worker", "capabilities": []}
            ).encode(),
        )

        assert route_from_node(directory, "worker", "edge") == Route(
            Address("worker"), "agents/by-name/worker/task"
        )

    def test_a_bare_name_also_placed_somewhere_is_not_guessed(self) -> None:
        directory = _heard(("worker", None, False), ("worker", "pc", True))

        route = route_from_node(directory, "worker", "edge")

        assert isinstance(route, str)
        assert "`worker@pc`" in route
        assert route_from_node(directory, "worker@pc", "edge") == Route(
            Address("worker", "pc"), "agents/at/pc/worker/task"
        )

    def test_an_address_on_main_is_refused_from_a_node(self) -> None:
        route = route_from_node(AgentDirectory(), "weather@main", "edge")

        assert route == "'weather' is on main, which takes no task from an agent on a node."

    def test_a_name_no_manifest_has_named_goes_by_name(self) -> None:
        assert route_from_node(AgentDirectory(), "elsewhere", "edge") == Route(
            Address("elsewhere"), "agents/by-name/elsewhere/task"
        )

    def test_an_address_on_a_node_not_heard_of_is_refused(self) -> None:
        route = route_from_node(AgentDirectory(), "elsewhere@pi", "edge")

        assert route == "No agent 'elsewhere' is known on node 'pi'."


class _Sent:
    """Stands in for the actor behind NodeHosted: records the manifest it is handed."""

    sent: dict[str, Any] | None = None

    async def _send_manifest(self, manifest: dict[str, Any]) -> None:
        self.sent = manifest


class _HostedAgent(NodeHosted, _Sent):
    pass


class TestWhatAnAgentOnANodeSaysOfItself:
    async def test_its_manifest_names_its_node_and_how_to_reach_it(self) -> None:
        agent = _HostedAgent()  # pyright: ignore[reportAbstractUsage]  # typed as an Actor, used for its manifest alone
        agent._node = "pi"

        await agent._send_manifest({"name": "counter"})

        assert agent.sent == {"name": "counter", "node": "pi", "tasks_by_node": True}
