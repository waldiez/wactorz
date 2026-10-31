"""An agent on main and a second agent of its name on a node, each reached alone by its address.

`/agents copy` is the way a person asks for the second one. Over a real broker
the copy is spawned on the node as main spawns any agent there, under an id of
its own, and both then answer to their own address: one that also took the
other's tasks, or ran under its id, would show here.
"""

import asyncio
import json
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from wactorz.agents.catalog_agent import CatalogAgent
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.commands import agents as agent_cmds
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.core import topic_bus
from wactorz.core.topic_bus import TopicBus
from wactorz.web import api_reset

from .conftest import FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that says where it runs, and counts the tasks it has had.
WHERE_AM_I = """
async def handle_task(agent, payload):
    tasks = (agent.recall("tasks") or 0) + 1
    agent.persist("tasks", tasks)
    return {"result": agent.node, "tasks": tasks}
"""


async def _ask(main: MainActor, address: str) -> dict[str, Any]:
    reply = await asyncio.wait_for(
        main.delegation.delegate_task(address, "", timeout=30), timeout=60
    )
    assert reply is not None, f"'{address}' never answered"
    return reply


async def _ready(main: MainActor, node: FastNode) -> None:
    """Wait until main knows the node takes ids and tasks of its own."""
    await until(lambda: main.nodes.takes_actor_ids(node.node_name), "main reading the manifest")
    await until(lambda: main.nodes.takes_tasks_by_node(node.node_name), "tasks by node")


@pytest.fixture(name="shared")
def shared_fixture() -> str:
    """A name of its own per test: what an agent leaves retained outlives the test."""
    return f"tally-{uuid.uuid4().hex[:8]}"


async def _copied(main: MainActor, node: FastNode, shared: str) -> str:
    """Run ``shared`` on main, copy it to ``node``, and wait for both; what the copy said."""
    await _ready(main, node)
    await main._spawn_from_config({"name": shared, "code": WHERE_AM_I}, save=True)
    said = await agent_cmds.copy_agent(CommandContext(actor=main), f"{shared} {node.node_name}")
    await until(
        lambda: shared in main._known_nodes[node.node_name].get("agents", []),
        f"main seeing '{shared}' on {node.node_name}",
    )
    return said


async def _on_the_node(main: MainActor, node: FastNode, shared: str) -> CatalogAgent:
    """Run ``shared`` on ``node``, and a catalog that holds a recipe of that name."""
    await _ready(main, node)
    await main._spawn_from_config(
        {"name": shared, "code": WHERE_AM_I, "node": node.node_name}, save=True
    )
    await until(
        lambda: shared in main._known_nodes[node.node_name].get("agents", []),
        f"main seeing '{shared}' on {node.node_name}",
    )
    spawned = await main.spawn(CatalogAgent, name=f"catalog-{uuid.uuid4().hex[:8]}")
    catalog = cast("CatalogAgent", spawned)  # spawn is typed to the base class
    catalog._catalog[shared] = {"name": shared, "type": "dynamic", "code": WHERE_AM_I}
    return catalog


class TestACatalogSpawnOfANameOnANode:
    async def test_it_asks_and_starts_nothing_on_main(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        catalog = await _on_the_node(main, node, shared)

        result = await catalog._action_spawn(shared, {})

        assert result["ok"] is False
        assert result["elsewhere"] == [f"{shared}@{node.node_name}"]
        assert main._registry is not None and main._registry.find_by_name(shared) is None
        assert main._get_spawn_registry().at(shared, "main") is None

    async def test_alongside_adds_a_second_under_its_own_id(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        catalog = await _on_the_node(main, node, shared)

        result = await catalog._action_spawn(shared, {"alongside": True})

        registry = main._get_spawn_registry()
        here, there = registry.at(shared, "main"), registry.at(shared, node.node_name)
        assert result["ok"] is True
        assert here is not None and there is not None
        assert here["actor_id"] != there["actor_id"]
        assert node.get(shared) is not None

    async def test_replace_moves_it_to_main_under_the_same_id(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        catalog = await _on_the_node(main, node, shared)
        before = main._get_spawn_registry().at(shared, node.node_name)

        result = await catalog._action_spawn(shared, {"replace": True})

        await until(lambda: node.get(shared) is None, f"'{shared}' leaving the node")
        registry = main._get_spawn_registry()
        here = registry.at(shared, "main")
        assert result["ok"] is True
        assert before is not None and here is not None
        assert here["actor_id"] == before["actor_id"]
        assert registry.at(shared, node.node_name) is None


class TestAResetOfOneOfTwo:
    async def test_the_other_keeps_its_state(
        self, main: MainActor, node: FastNode, shared: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _copied(main, node, shared)
        monkeypatch.setattr(api_reset.runtime, "registry", main._registry)
        monkeypatch.setattr(api_reset.ws, "broadcast", AsyncMock())
        await until(lambda: main.nodes.takes_resets(node.node_name), "the node taking resets")
        address = f"{shared}@{node.node_name}"
        await _ask(main, address)
        await _ask(main, f"{shared}@main")
        request = MagicMock()
        request.json = AsyncMock(return_value={"scope": "state", "agent": address})

        response = await api_reset.reset_handler(request)

        assert response.status == 200
        assert json.loads(cast("bytes", response.body))["nodes"] == {node.node_name: "cleared"}
        assert (await _ask(main, address))["tasks"] == 1
        assert (await _ask(main, f"{shared}@main"))["tasks"] == 2


class TestACopyOnANode:
    async def test_each_is_reached_alone_by_its_address(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        said = await _copied(main, node, shared)

        there = [await _ask(main, f"{shared}@{node.node_name}") for _ in range(2)]
        here = await _ask(main, f"{shared}@main")

        assert said == f"Spawned '{shared}' on node '{node.node_name}'"
        assert [(r["result"], r["tasks"]) for r in there] == [
            (node.node_name, 1),
            (node.node_name, 2),
        ]
        # Had the one here heard the node's tasks, this would be its third.
        assert (here["result"], here["tasks"]) == ("local", 1)

    async def test_they_are_two_agents_under_two_ids(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        await _copied(main, node, shared)

        registry = main._get_spawn_registry()
        here, there = registry.at(shared, "main"), registry.at(shared, node.node_name)
        running = main._registry.find_by_name(shared) if main._registry else None

        assert here is not None and there is not None
        assert running is not None and running.actor_id == here["actor_id"]
        assert there["actor_id"] != here["actor_id"]
        assert node.get(shared) is not None
        assert node.get(shared).actor_id == there["actor_id"]  # pyright: ignore[reportOptionalMemberAccess]

    async def test_the_planner_can_see_both(
        self, main: MainActor, node: FastNode, shared: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        await _copied(main, node, shared)
        registry = main._get_spawn_registry()
        here, there = registry.at(shared, "main"), registry.at(shared, node.node_name)
        assert here is not None and there is not None

        await until(
            lambda: (
                {c.actor_id for c in bus.registry.named(shared)}
                == {here["actor_id"], there["actor_id"]}
            ),
            "a contract for each",
        )

    async def test_the_bare_name_is_refused_with_both_addresses(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        await _copied(main, node, shared)

        reply = await _ask(main, shared)

        assert f"`{shared}@main`" in reply["error"]
        assert f"`{shared}@{node.node_name}`" in reply["error"]

    async def test_a_second_copy_onto_the_same_node_is_refused(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        await _copied(main, node, shared)

        said = await agent_cmds.copy_agent(
            CommandContext(actor=main), f"{shared}@main {node.node_name}"
        )

        assert f"{shared}@{node.node_name} already runs" in said
        assert len(main._get_spawn_registry().named(shared)) == 2

    async def test_main_holds_each_ones_manifest(
        self, main: MainActor, node: FastNode, shared: str
    ) -> None:
        await _copied(main, node, shared)
        registry = main._get_spawn_registry()
        here, there = registry.at(shared, "main"), registry.at(shared, node.node_name)
        assert here is not None and there is not None

        def held() -> dict[str, str]:
            return {
                str(m.get("actor_id")): str(m.get("node") or "")
                for m in main._agent_manifests.values()
                if m.get("name") == shared
            }

        await until(lambda: len(held()) == 2, "main holding both manifests")

        assert held() == {here["actor_id"]: "", there["actor_id"]: node.node_name}
