"""A node agent's question, answered from the dashboard's API, kept as a label.

Every hop is a real one: the agent on the node publishes its question book,
the server's listener puts it in the ledger the API lists, the API's answer
goes back over the broker to the node, the agent's book says it was answered,
and main's recorder writes the label that `GET /api/labels` reads. A topic or a
field that one side spells differently fails here.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.agents.main.actor import MainActor
from wactorz.web import questions, runtime
from wactorz.web.app import build_app
from wactorz.web.mqtt import mqtt_listener

from .conftest import WAIT_S, FastNode, until

T = TypeVar("T")

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that asks about a source and answers its task with the pick.
ASKS = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    picked = await agent.ask_operator(
        "Maintenance, or a real problem?",
        options=["Maintenance", "Real problem", "Not sure"],
        timeout=50,
        label={
            "source": payload["source"],
            "start": payload["start"],
            "values": {"Maintenance": "maintenance", "Real problem": "fault"},
        },
    )
    return {"result": "picked:" + picked}
"""


@pytest.fixture(name="server")
async def server_fixture(
    broker: tuple[str, int], main: MainActor, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    """The dashboard's API, with the server's own listener on the broker filling its ledger."""
    host, port = broker
    monkeypatch.setattr(runtime, "MQTT_BROKER", host)
    monkeypatch.setattr(runtime, "MQTT_PORT", port)
    monkeypatch.setattr(runtime, "registry", main._registry)
    monkeypatch.setattr(questions, "_books", {})
    listening = asyncio.create_task(mqtt_listener())
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        await until(lambda: runtime.mqtt_client_ref is not None, "the server connected")
        yield client
    finally:
        await client.close()
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def _open_question(client: TestClient, agent: str) -> dict[str, Any] | None:
    listed = await (await client.get("/api/questions")).json()
    return next((q for q in listed["open"] if q["agent"] == agent), None)


async def _labels(client: TestClient, source: str) -> list[dict[str, Any]] | None:
    found = (await (await client.get("/api/labels", params={"source": source})).json())["labels"]
    return found or None


async def _eventually(fetch: Callable[[], Awaitable[T | None]], what: str) -> T:
    """What ``fetch`` returns once it returns something, asking again until then."""
    deadline = asyncio.get_running_loop().time() + WAIT_S
    while (got := await fetch()) is None:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"within {WAIT_S:g}s, never: {what}")
        await asyncio.sleep(0.1)
    return got


async def test_a_node_agents_question_answered_from_the_api_becomes_a_label(
    main: MainActor, node: FastNode, server: TestClient
) -> None:
    source = f"sensors/imu/{uuid.uuid4().hex[:8]}"
    await main._spawn_remote(
        {"name": "asker", "type": "dynamic", "code": ASKS}, node.node_name, save=True
    )
    await until(
        lambda: "asker" in main._known_nodes.get(node.node_name, {}).get("agents", []),
        "main seeing 'asker' on the node",
    )
    asking = asyncio.create_task(
        main.delegation.delegate_task("asker", {"source": source, "start": 1000.0}, timeout=60)
    )

    found = await _eventually(
        lambda: _open_question(server, "asker"), "the ledger listing the question"
    )
    assert found["label"]["source"] == source

    answered = await server.post(
        f"/api/questions/{found['agent_id']}/{found['id']}/answer", json={"option": "Maintenance"}
    )

    assert answered.status == 200, await answered.text()
    reply = await asyncio.wait_for(asking, 60)
    assert reply is not None and reply.get("result") == "picked:Maintenance"

    (label,) = await _eventually(lambda: _labels(server, source), "the answer kept as a label")
    assert (label["value"], label["start"], label["end"]) == ("maintenance", 1000.0, None)
    assert label["question_id"] == found["id"]
    assert label["node"] == node.node_name
    # Sent from the dashboard's own API, so a person's, not anyone's on the broker.
    assert label["verified"] is True
