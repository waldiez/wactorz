"""``GET /api/nodes``: the nodes main knows, for a page that opens after they said so.

A node's manifest is retained, so it reaches the server once, when it
subscribes; the dashboard's live feed never carries it to a page opened later.
This is where that page reads it.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import api_system
from wactorz.web.app import build_app


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.parametrize("path", ["/api/nodes", "/nodes"])
async def test_every_known_node_with_its_machine(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    listed: list[dict[str, Any]] = [
        {"node": "rpi", "online": True, "manifest": {"manifest_v": 1, "arch": "aarch64"}}
    ]
    monkeypatch.setattr(api_system, "known_nodes", lambda: listed)

    response = await client.get(path)

    assert response.status == 200
    assert await response.json() == {"nodes": listed}


async def test_without_main_there_are_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_system, "known_nodes", list)

    assert await (await client.get("/api/nodes")).json() == {"nodes": []}
