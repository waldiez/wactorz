# SPDX-License-Identifier: Apache-2.0
"""A generated program writes Wactorz's topics bare, and works over a real broker either way.

Run by `make test-broker` twice: with no namespace, and with ``WACTORZ_NAMESPACE``
set. One program publishes on ``agents/<id>/data/level`` and declares it; another
subscribes to ``agents/+/data/level`` and declares that. Both are written the way a model writes
them, bare. What travels is the namespaced topic, the reader hears the writer,
the wiring the dashboard is sent pairs them and shows them bare, and so does the
frame the dashboard is sent for a message on it.
"""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import aiomqtt
import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.main.actor import MainActor
from wactorz.core import topic_bus
from wactorz.core.topic_bus import TopicBus
from wactorz.core.topics import NAMESPACE, wire_topic
from wactorz.core.wiring import wiring
from wactorz.web import mqtt as web_mqtt
from wactorz.web.wiring import displayed

from .conftest import WAIT_S, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

WRITER = """
async def setup(agent):
    agent.declare_contract(publishes=[f"agents/{agent.actor_id}/data/level"])


async def process(agent):
    await agent.publish(f"agents/{agent.actor_id}/data/level", {"level": 3})
"""

READER = """
async def setup(agent):
    async def on_level(payload):
        agent.state["heard"] = agent.state.get("heard", 0) + 1

    agent.subscribe("agents/+/data/level", on_level)
    agent.declare_contract(subscribes=["agents/+/data/level"])
"""


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> TopicBus:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    return bus


async def _spawn(main: MainActor, name: str, code: str) -> DynamicAgent:
    spawned = await main._spawn_local_from_config(  # pyright: ignore[reportPrivateUsage]
        {"name": name, "type": "dynamic", "code": code, "poll_interval": 0.3},
        register=False,
        from_registry=True,
    )
    assert isinstance(spawned, DynamicAgent)
    return spawned


async def _first_on(broker: tuple[str, int], topic: str, wait: float) -> str | None:
    """The topic of the first message on the filter ``topic``, exactly as written; None if none."""
    host, port = broker
    async with aiomqtt.Client(host, port) as client:
        await client.subscribe(topic)

        async def first() -> str:
            async for message in client.messages:
                return str(message.topic)
            return ""

        try:
            return await asyncio.wait_for(first(), wait)
        except asyncio.TimeoutError:
            return None


async def test_a_program_written_bare_works_under_the_namespace(
    broker: tuple[str, int], main: MainActor, bus: TopicBus
) -> None:
    suffix = uuid.uuid4().hex[:6]
    writer = await _spawn(main, f"writer-{suffix}", WRITER)
    reader = await _spawn(main, f"reader-{suffix}", READER)
    written = f"agents/{writer.actor_id}/data/level"

    # What travels is the namespaced topic; with a namespace, the bare one carries nothing.
    arrived = await _first_on(broker, wire_topic("agents/+/data/level"), WAIT_S)
    assert arrived is not None and arrived == wire_topic(written)
    if NAMESPACE:
        assert await _first_on(broker, "agents/+/data/level", 2.0) is None

    # The reader, which subscribed bare, hears the writer.
    await until(lambda: reader._api.state.get("heard", 0) > 0, "the reader hearing the writer")  # pyright: ignore[reportPrivateUsage]

    # The wiring pairs them on one topic, and the page is sent it bare.
    graph = displayed(wiring(bus.registry.all_contracts(), {}, pointers=lambda _name: {}, asked={}))
    entry = next(t for t in graph["topics"] if t["topic"] == written)
    assert reader.actor_id in entry["consumers"]
    assert ("unread", written) not in {(w["kind"], w.get("topic")) for w in graph["warnings"]}

    # And the frame the dashboard is sent for a message on it names it bare.
    broadcast = AsyncMock()
    with patch("wactorz.web.ws.broadcast", new=broadcast):
        await web_mqtt.broadcast_mqtt_msg(arrived, json.dumps({"level": 3}))
    frame: dict[str, Any] = broadcast.call_args.args[0]
    assert frame["topic"] == written
