"""What main and a node agree on, exercised over a real broker.

Each test is one thing a deployment depends on: the node is seen, a signed
command is obeyed, an unsigned one is not, a task comes back answered, and a
stop is carried out.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

import wactorz
from wactorz.agents.main.actor import MainActor
from wactorz.core.cancellation import cancel_until_done

from .conftest import WAIT_S, FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that answers a task with what it was sent.
ECHO = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    return {"result": "echo:" + str(payload.get("text", ""))}
"""


def _echo(name: str = "echo") -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": ECHO}


def _agents_main_sees(main: MainActor, node: FastNode) -> list[str]:
    return list(main._known_nodes.get(node.node_name, {}).get("agents", []))


async def _spawned(main: MainActor, node: FastNode, name: str = "echo") -> None:
    await main._spawn_remote(_echo(name), node.node_name, save=False)
    await until(lambda: name in _agents_main_sees(main, node), f"main seeing '{name}' on the node")


class TestTheNodeIsSeen:
    async def test_main_learns_the_node_and_its_version(
        self, main: MainActor, node: FastNode
    ) -> None:
        known = main._known_nodes[node.node_name]

        assert known["version"] == wactorz.__version__
        assert known["runtime"] == "node"


class TestASignedCommandIsObeyed:
    async def test_main_spawns_an_agent_on_the_node(self, main: MainActor, node: FastNode) -> None:
        await _spawned(main, node)

        assert node.get("echo") is not None

    async def test_main_stops_it_again(self, main: MainActor, node: FastNode) -> None:
        await _spawned(main, node)

        await main._mqtt_publish(f"nodes/{node.node_name}/stop", {"name": "echo"}, qos=1)

        await until(lambda: node.get("echo") is None, "the node stopping 'echo'")
        await until(lambda: "echo" not in _agents_main_sees(main, node), "main seeing 'echo' gone")


class TestAnUnsignedCommandIsRefused:
    async def test_a_spawn_from_another_client_starts_nothing(
        self, broker: tuple[str, int], main: MainActor, node: FastNode
    ) -> None:
        # Anything on the broker can publish to the node's spawn topic. Only main
        # holds the key, so a spawn from elsewhere carries no signature the node
        # accepts, and the code in it is never run.
        host, port = broker
        async with aiomqtt.Client(host, port) as client:
            await client.publish(
                f"nodes/{node.node_name}/spawn", json.dumps(_echo("intruder")), qos=1
            )
        # A signed spawn sent after it, so there is something to wait for: once
        # the node has acted on this one, it has also read the one before it.
        await _spawned(main, node, "echo")

        assert node.get("intruder") is None
        assert "intruder" not in _agents_main_sees(main, node)


class TestATaskComesBackAnswered:
    async def test_main_asks_an_agent_on_the_node_and_gets_its_reply(
        self, main: MainActor, node: FastNode
    ) -> None:
        await _spawned(main, node)

        reply = await asyncio.wait_for(
            main.delegation.delegate_task("echo", "ping", timeout=30), timeout=60
        )

        assert reply is not None
        assert reply.get("result") == "echo:ping"


class TestANodeThatRestarts:
    async def test_it_brings_its_agents_back_from_what_main_retained(
        self,
        broker: tuple[str, int],
        main: MainActor,
        node: FastNode,
        tmp_path: Path,
    ) -> None:
        # Main keeps each node's desired state retained on the broker, so a node
        # that reboots runs what it should without main saying it again. The
        # restarted node gets an empty state directory, so the retained message
        # is the one place it can learn that from.
        await main._spawn_remote(_echo(), node.node_name, save=True)
        await until(lambda: node.get("echo") is not None, "the node running 'echo'")
        await node.shutdown()
        await until(lambda: not node._running, "the first node process stopping")

        host, port = broker
        again = FastNode(host, port, node.node_name, state_dir=str(tmp_path / "node-again"))
        running = asyncio.create_task(again.run())
        try:
            await until(lambda: again.get("echo") is not None, "the restarted node running 'echo'")
        finally:
            await again.shutdown()
            await cancel_until_done(running, timeout=WAIT_S)
