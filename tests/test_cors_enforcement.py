"""A page on another origin cannot act on this server.

Driven through the real application so both spellings of every route are
covered: nearly every path is registered twice, under `/api/x` and a bare `/x`,
which is why the check is middleware and not a per-route decorator.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import app

EVIL = "https://evil.example.com"


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient[Any, Any]]:
    async with TestClient(TestServer(app.build_app())) as test_client:
        yield test_client


class TestStateChangingRequests:
    @pytest.mark.parametrize("path", ["/api/reset", "/reset", "/api/cost/reset", "/cost/reset"])
    async def test_another_origin_is_refused_on_both_spellings(
        self, client: TestClient[Any, Any], path: str
    ) -> None:
        resp = await client.post(path, headers={"Origin": EVIL}, json={})

        assert resp.status == 403

    async def test_deleting_an_actor_too(self, client: TestClient[Any, Any]) -> None:
        resp = await client.delete("/api/actors/a1", headers={"Origin": EVIL})

        assert resp.status == 403

    async def test_a_caller_with_no_origin_is_not_a_browser_and_is_let_through(
        self, client: TestClient[Any, Any]
    ) -> None:
        # curl, scripts and the Python client send none. Refusing them would
        # break every integration while protecting nobody.
        resp = await client.post("/api/cost/reset", json={})

        assert resp.status != 403


class TestReads:
    async def test_the_response_names_no_origin_for_a_stranger(
        self, client: TestClient[Any, Any]
    ) -> None:
        # A read needs no refusal: withholding the header is what stops the
        # browser handing the body to the page.
        resp = await client.get("/api/feed", headers={"Origin": EVIL})

        assert "Access-Control-Allow-Origin" not in resp.headers

    async def test_the_answer_is_marked_as_depending_on_the_origin(
        self, client: TestClient[Any, Any]
    ) -> None:
        # Without Vary a shared cache could hand one caller's grant to another.
        resp = await client.get("/api/feed", headers={"Origin": EVIL})

        assert resp.headers.get("Vary") == "Origin"


class TestPreflight:
    async def test_a_stranger_is_not_preapproved(self, client: TestClient[Any, Any]) -> None:
        resp = await client.options("/api/reset", headers={"Origin": EVIL})

        assert resp.headers.get("Access-Control-Allow-Origin") != "*"
        assert "Access-Control-Allow-Origin" not in resp.headers


class TestTheWebsocketHandshake:
    async def test_a_page_on_another_origin_cannot_open_the_socket(
        self, client: TestClient[Any, Any]
    ) -> None:
        # A WebSocket is exempt from CORS entirely, so the HTTP rules above
        # protect nothing here — an unchecked handshake leaves the live feed
        # readable and the command channel writable from any page.
        resp = await client.get(
            "/ws",
            headers={
                "Origin": EVIL,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            },
        )

        assert resp.status == 403
