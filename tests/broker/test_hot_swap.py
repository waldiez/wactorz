"""A moved pointer reaches the agents that follow it, on main and on a node, over a real broker.

Every hop is a real one: the pointer watcher finds the move, main swaps the
agent it runs and sends the node its signed command, the node fetches the
version from main's store over the broker, swaps its agent and answers, and
main writes the swap into the provenance graph. Each agent answers a task with
the version it serves, so the test reads the swap from the outside.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.model_swaps import swaps_of
from wactorz.core.persistence.stores import get_db
from wactorz.core.provenance import Provenance

from .conftest import WAIT_S, FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that keeps its model in state, takes a new one through the hook,
#: and answers a task with the version it serves and the bytes it holds.
SERVES = """
async def setup(agent):
    agent.state['weights'] = await agent.load_model('model')


async def on_model(agent, key, model):
    agent.state['weights'] = model


async def handle_task(agent, payload):
    return {
        "result": agent.artifacts['model']['version'],
        "weights": agent.state['weights'].decode(),
    }
"""


def _serving(name: str) -> dict[str, Any]:
    return {"name": name, "type": "dynamic", "code": SERVES, "artifacts": {"model": "imu"}}


def _store_two_versions(main: MainActor, tmp_path: Path) -> ArtifactStore:
    """``imu`` in two versions in main's store, with ``active`` on the first."""
    store = main.artifact_service.store
    for version in (1, 2):
        weights = tmp_path / f"imu-{version}.bin"
        weights.write_bytes(f"imu weights {version}".encode())
        store.add(weights, "imu", format_name="raw")
    store.move("active", "imu:1", by="test")
    return store


async def _served(main: MainActor, name: str) -> dict[str, Any]:
    reply = await asyncio.wait_for(main.delegation.delegate_task(name, {}, timeout=30), timeout=60)
    assert reply is not None, f"{name} did not answer"
    return reply


async def _serves_version(main: MainActor, name: str, version: int) -> None:
    """Wait for ``name`` to answer with ``version`` and the bytes that version holds."""

    async def poll() -> None:
        while True:
            reply = await _served(main, name)
            if reply.get("result") == version and reply.get("weights") == f"imu weights {version}":
                return
            await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(poll(), WAIT_S)
    except asyncio.TimeoutError:
        pytest.fail(f"within {WAIT_S:g}s, {name} never served imu:{version}")


async def test_agents_on_main_and_on_a_node_follow_the_active_pointer(
    main: MainActor, node: FastNode, tmp_path: Path
) -> None:
    store = _store_two_versions(main, tmp_path)
    await main._spawn_local_from_config(_serving("here"), register=True)
    await main._spawn_remote(_serving("there"), node.node_name, save=True)
    await until(
        lambda: "there" in main._known_nodes.get(node.node_name, {}).get("agents", []),
        "main seeing 'there' on the node",
    )
    await _serves_version(main, "here", 1)
    await _serves_version(main, "there", 1)
    # The watcher has published where the pointers stand, so the move below
    # is a change it finds rather than the first look.
    await main.artifact_service.publish_pointers()

    store.move("active", "imu:2", by="dashboard", reason="better recall")
    await main.artifact_service.publish_pointers()

    await _serves_version(main, "here", 2)
    await _serves_version(main, "there", 2)
    db = get_db()
    assert db is not None
    graph = Provenance(db)
    await until(lambda: len(swaps_of(graph, "imu")) == 2, "both swaps in the graph")
    swapped = {(s["agent"], s["host"]): (s["from"], s["to"]) for s in swaps_of(graph, "imu")}
    assert swapped == {("here", "main"): (1, 2), ("there", node.node_name): (1, 2)}

    # Rollback is the same path the other way.
    store.rollback("imu", by="dashboard", reason="too many alarms")
    await main.artifact_service.publish_pointers()

    await _serves_version(main, "here", 1)
    await _serves_version(main, "there", 1)
