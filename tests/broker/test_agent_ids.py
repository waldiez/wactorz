"""An agent's id goes with it: minted on main, kept on a node and back again.

Main sends the id in the spawn and the node builds the agent under it, so over
a real broker the two ends have to agree on the field and on the agent that
arrives. One that took an id of its own would answer under another topic.
"""

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.core.identity import derive_actor_id

from .conftest import FastNode, until
from .test_moving_and_asking import COUNTER, _agents_main_sees, _on_main

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]


class TestAnIdThatTravels:
    async def test_the_agent_keeps_its_id_out_and_home(
        self, main: MainActor, node: FastNode
    ) -> None:
        registry = main._registry
        assert registry is not None
        await _on_main(main, "counter", COUNTER)
        running = registry.find_by_name("counter")
        assert running is not None
        minted = running.actor_id
        assert minted != derive_actor_id("counter"), "a new agent gets an id of its own"
        assert main._registered_actor_id("counter") == minted

        out = await main.migrate_agent("counter", node.node_name)

        assert out["success"], out
        await until(lambda: node.get("counter") is not None, "the node running 'counter'")
        there = node.get("counter")
        assert there is not None
        assert there.actor_id == minted
        # The node runs it before main has its confirmation; a move is asked
        # once main has it, as a second move is refused until then.
        await until(
            lambda: main._get_spawn_registry().at("counter", node.node_name) is not None,
            "main to confirm the move",
        )
        assert main._registered_actor_id("counter") == minted

        home = await main.migrate_agent("counter", "local")

        assert home["success"], home
        await until(lambda: registry.find_by_name("counter") is not None, "main running it again")
        await until(
            lambda: "counter" not in _agents_main_sees(main, node), "main seeing it gone there"
        )
        back = registry.find_by_name("counter")
        assert back is not None
        assert back.actor_id == minted
