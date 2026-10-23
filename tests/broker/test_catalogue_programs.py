"""A catalogue program's own broker connection, on main and on a node.

Some programs open a connection of their own, besides the one their host
publishes through. It goes through the package's `mqtt_client`, so it takes the
broker, the account and TLS from where its host does: main's settings on main,
the node's `.env` on a node. Here the timeseries collector runs on each over a
real broker, and a reading published there has to reach it.
"""

import asyncio
import json
import uuid
from typing import Any

import aiomqtt
import pytest

from wactorz.agents.catalog_agent import _load_recipe
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.main.actor import MainActor

from .conftest import WAIT_S, FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]


def _collector(name: str) -> dict[str, Any]:
    code = _load_recipe("timeseries_collector_agent.py")
    assert code is not None
    return {"name": name, "type": "dynamic", "code": code, "trusted": True}


def _received(agent: DynamicAgent | None) -> int:
    if agent is None:
        return 0
    return int(agent._api.state.get("total_received", 0))  # pyright: ignore[reportPrivateUsage]


async def _publish_until_heard(
    broker: tuple[str, int], heard: Any, what: str, timeout: float = WAIT_S
) -> None:
    """Publish a reading every so often until ``heard()`` holds.

    Repeated rather than sent once: the program subscribes on a connection of
    its own, some time after it starts, and a reading sent before that is lost.
    """
    host, port = broker
    topic = f"sensors/it-{uuid.uuid4().hex[:8]}/temperature"
    async with aiomqtt.Client(host, port) as client:

        async def keep_publishing() -> None:
            while not heard():
                await client.publish(topic, json.dumps({"value": 21.5}))
                await asyncio.sleep(0.3)

        try:
            await asyncio.wait_for(keep_publishing(), timeout)
        except asyncio.TimeoutError:
            pytest.fail(f"within {timeout:g}s, never: {what}")


async def test_on_main_the_program_hears_the_broker(
    broker: tuple[str, int], main: MainActor
) -> None:
    name = f"collector-{uuid.uuid4().hex[:6]}"
    spawned = await main._spawn_local_from_config(  # pyright: ignore[reportPrivateUsage]
        _collector(name), register=False, from_registry=True
    )
    assert isinstance(spawned, DynamicAgent)

    await _publish_until_heard(
        broker, lambda: _received(spawned) > 0, "the collector on main counting a reading"
    )


async def test_on_a_node_the_program_hears_the_broker(
    broker: tuple[str, int], main: MainActor, node: FastNode
) -> None:
    name = f"collector-{uuid.uuid4().hex[:6]}"
    await main._spawn_remote(_collector(name), node.node_name, save=False)  # pyright: ignore[reportPrivateUsage]
    await until(lambda: node.get(name) is not None, f"the node running '{name}'")

    await _publish_until_heard(
        broker,
        lambda: _received(node.get(name)) > 0,
        "the collector on the node counting a reading",
    )
