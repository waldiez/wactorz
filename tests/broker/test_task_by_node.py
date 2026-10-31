"""Two nodes running an agent of one name, each reached alone by its address.

A task by name reaches every node that runs the name. One on a node's own
topic, ``agents/at/<node>/<name>/task``, reaches that node alone: from main,
which reads where agents run from the nodes' heartbeats and manifests, and
from an agent on another node, which reads it from the agents' manifests.
Over a real broker both ends are the production ones, so a node that also
answered the other's task would show here.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.core import node_signing
from wactorz.core.cancellation import cancel_until_done

from .conftest import WAIT_S, FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that says which node answered, and counts the tasks it has had.
WHERE_AM_I = """
async def handle_task(agent, payload):
    tasks = (agent.recall("tasks") or 0) + 1
    agent.persist("tasks", tasks)
    return {"result": agent.node, "tasks": tasks}
"""

#: An agent that only asks: the test calls its `send_to` as its own code would.
ASKER = ""


class _Node(FastNode):
    """A node that notes when main has told it which agents it should run.

    Main does so when it first hears the node, and the node then stops whatever
    the list leaves out: an agent started here before that would be stopped.
    """

    told_desired = False

    async def _on_desired_state(self, topic_str: str, data: Any, msg: Any) -> None:
        await super()._on_desired_state(topic_str, data, msg)
        self.told_desired = True


def _a_node(broker: tuple[str, int], state: Path, monkeypatch: pytest.MonkeyPatch) -> _Node:
    """A node with its own name and key, not yet started."""
    host, port = broker
    name = f"it-{uuid.uuid4().hex[:8]}"
    # Read when the runner is built, so each node takes its own.
    monkeypatch.setenv("WACTORZ_NODE_KEY", node_signing.node_key(name))
    monkeypatch.setenv("WACTORZ_NODE_SIGNING", "enforce")
    return _Node(host, port, name, state_dir=str(state / name))


@pytest.fixture(name="shared")
def shared_fixture() -> str:
    """The name both nodes' agents share: one of its own per test, since what an agent
    leaves retained outlives the test on the broker, and another test's agent of that
    name would find it."""
    return f"tally-{uuid.uuid4().hex[:8]}"


@pytest.fixture(name="two_nodes")
async def two_nodes_fixture(
    broker: tuple[str, int],
    main: MainActor,
    shared: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FastNode, FastNode]]:
    """Two running nodes, each with an agent called `shared` started there: main's registry keeps neither."""
    nodes = [_a_node(broker, tmp_path, monkeypatch) for _ in range(2)]
    running = [asyncio.create_task(node.run()) for node in nodes]
    try:
        for node in nodes:
            await until(
                lambda n=node: n.node_name in main._known_nodes, f"main hearing {node.node_name}"
            )
            await until(
                lambda n=node: main.nodes.takes_tasks_by_node(n.node_name),
                f"main reading {node.node_name}'s manifest",
            )
            await until(lambda n=node: n.told_desired, f"main telling {node.node_name} what to run")
            # An id of its own, as main mints for every agent it spawns: two
            # agents of one name under one id would share every topic.
            await node.spawn_agent(
                {"name": shared, "code": WHERE_AM_I, "actor_id": str(uuid.uuid4())}
            )
        for node in nodes:
            await until(
                lambda n=node: shared in main._known_nodes[n.node_name].get("agents", []),
                f"main seeing '{shared}' on {node.node_name}",
            )
        yield nodes[0], nodes[1]
    finally:
        for node in nodes:
            await node.shutdown()
        for task in running:
            await cancel_until_done(task, timeout=WAIT_S)


async def _ask(main: MainActor, address: str) -> dict[str, Any]:
    reply = await asyncio.wait_for(
        main.delegation.delegate_task(address, "", timeout=30), timeout=60
    )
    assert reply is not None, f"'{address}' never answered"
    return reply


class TestFromMain:
    async def test_each_is_reached_alone_by_its_address(
        self, main: MainActor, two_nodes: tuple[FastNode, FastNode], shared: str
    ) -> None:
        first, second = two_nodes

        twice = [await _ask(main, f"{shared}@{second.node_name}") for _ in range(2)]
        once = await _ask(main, f"{shared}@{first.node_name}")

        assert [reply["result"] for reply in twice] == [second.node_name] * 2
        assert [reply["tasks"] for reply in twice] == [1, 2]
        # Had the first heard the second's tasks, this would be its third.
        assert (once["result"], once["tasks"]) == (first.node_name, 1)

    async def test_the_bare_name_is_refused_with_both_addresses(
        self, main: MainActor, two_nodes: tuple[FastNode, FastNode], shared: str
    ) -> None:
        first, second = two_nodes

        reply = await _ask(main, shared)

        assert f"`{shared}@{first.node_name}`" in reply["error"]
        assert f"`{shared}@{second.node_name}`" in reply["error"]
        # Neither ran it: each one's first task is still its first.
        assert (await _ask(main, f"{shared}@{first.node_name}"))["tasks"] == 1
        assert (await _ask(main, f"{shared}@{second.node_name}"))["tasks"] == 1


class TestFromAnAgentOnAnotherNode:
    async def test_it_reaches_the_one_its_address_names(
        self, main: MainActor, two_nodes: tuple[FastNode, FastNode], shared: str
    ) -> None:
        first, second = two_nodes
        await first.spawn_agent({"name": f"asker-{shared}", "code": ASKER})
        await until(
            lambda: first.directory.resolve(f"{shared}@{second.node_name}").found is not None,
            "the first node hearing where the second's agent runs",
        )
        asker = first.get(f"asker-{shared}")
        assert asker is not None

        # As the asker's own code would call it, from the first node.
        reply = await asyncio.wait_for(
            asker._api.send_to(f"{shared}@{second.node_name}", {}, timeout=30), timeout=60
        )

        assert reply is not None
        assert (reply["result"], reply["tasks"]) == (second.node_name, 1)
        # Had the first node's agent heard it too, this would be its second.
        assert (await _ask(main, f"{shared}@{first.node_name}"))["tasks"] == 1
