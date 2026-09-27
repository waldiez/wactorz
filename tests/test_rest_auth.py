"""The REST API key guards every route except the probe endpoints.

The guard lives in a middleware rather than in each handler: a route added
later is covered without anyone remembering to add a check to it.

An install with no key configured is unaffected — every route stays open.
"""

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.interfaces.chat.rest import RESTInterface

API_KEY = "secret-key"

# Every route the interface serves, minus /health. Kept explicit so a route
# added without a matching guard shows up as a failure here.
GUARDED_GET_PATHS = [
    "/metrics",
    "/ha-map",
    "/actors",
    "/actors/abc",
    "/actors/abc/metrics",
    "/agents",
]
GUARDED_POST_PATHS = [
    "/chat",
    "/agents/command",
    "/actors/abc/message",
    "/actors/abc/start",
]


class _Registry:
    """A registry holding no actors, so handlers return 404 rather than work."""

    def get(self, actor_id: str) -> None:
        return None

    def all_actors(self) -> list[Any]:
        return []

    def find_by_name(self, name: str) -> None:
        return None


class _MainActor:
    """The parts of MainActor that RESTInterface reaches for."""

    def __init__(self) -> None:
        self._registry = _Registry()

    async def process_user_input(self, message: str) -> str:
        return "reply"

    async def send_command(self, target: str, command: Any) -> None:
        return None

    async def send(self, actor_id: str, msg_type: Any, payload: Any) -> None:
        return None


def _interface(api_key: str | None) -> RESTInterface:
    return RESTInterface(_MainActor(), port=0, api_key=api_key)  # pyright: ignore[reportArgumentType]


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture(name="keyed_client")
async def keyed_client_fixture() -> Any:
    """A client for an interface that has an API key configured."""
    client = await _client(_interface(API_KEY).build_app())
    yield client
    await client.close()


@pytest.fixture(name="open_client")
async def open_client_fixture() -> Any:
    """A client for an interface with no API key — the default install."""
    client = await _client(_interface(None).build_app())
    yield client
    await client.close()


class TestAKeyIsRequired:
    @pytest.mark.parametrize("path", GUARDED_GET_PATHS)
    async def test_get_routes_refuse_a_request_with_no_key(
        self, keyed_client: TestClient, path: str
    ) -> None:
        assert (await keyed_client.get(path)).status == 401

    @pytest.mark.parametrize("path", GUARDED_POST_PATHS)
    async def test_post_routes_refuse_a_request_with_no_key(
        self, keyed_client: TestClient, path: str
    ) -> None:
        assert (await keyed_client.post(path, json={})).status == 401

    async def test_delete_refuses_a_request_with_no_key(self, keyed_client: TestClient) -> None:
        # The most destructive route, and it was open before the guard moved
        # off /chat.
        assert (await keyed_client.delete("/actors/abc")).status == 401

    async def test_a_wrong_key_is_refused(self, keyed_client: TestClient) -> None:
        resp = await keyed_client.get("/actors", headers={"X-API-Key": "wrong"})
        assert resp.status == 401


class TestAcceptedKeyForms:
    async def test_x_api_key_header(self, keyed_client: TestClient) -> None:
        resp = await keyed_client.get("/actors", headers={"X-API-Key": API_KEY})
        assert resp.status == 200

    async def test_authorization_bearer(self, keyed_client: TestClient) -> None:
        # Prometheus can only send standard auth headers, so a guarded
        # /metrics has to accept this form.
        resp = await keyed_client.get("/metrics", headers={"Authorization": f"Bearer {API_KEY}"})
        assert resp.status == 200

    async def test_bearer_scheme_is_matched_case_insensitively(
        self, keyed_client: TestClient
    ) -> None:
        resp = await keyed_client.get("/actors", headers={"Authorization": f"bearer {API_KEY}"})
        assert resp.status == 200

    async def test_a_bare_token_without_the_scheme_is_refused(
        self, keyed_client: TestClient
    ) -> None:
        resp = await keyed_client.get("/actors", headers={"Authorization": API_KEY})
        assert resp.status == 401


class TestHealthStaysReachable:
    async def test_health_needs_no_key(self, keyed_client: TestClient) -> None:
        # Container and uptime probes cannot carry one.
        resp = await keyed_client.get("/health")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"


class TestNoKeyConfigured:
    @pytest.mark.parametrize("path", ["/health", "/actors", "/agents", "/metrics"])
    async def test_every_route_stays_open(self, open_client: TestClient, path: str) -> None:
        assert (await open_client.get(path)).status == 200

    async def test_chat_still_answers(self, open_client: TestClient) -> None:
        resp = await open_client.post("/chat", json={"message": "hi"})
        assert resp.status == 200
        assert (await resp.json())["response"] == "reply"


class TestAnOpenInstallRefusesOtherSites:
    """With no key, the host and origin checks are all that keeps a web page out."""

    async def test_a_rebound_host_name_is_refused(self, open_client: TestClient) -> None:
        # What DNS rebinding looks like: a name the attacker controls, resolving here.
        resp = await open_client.get("/actors", headers={"Host": "attacker.example:8000"})

        assert resp.status == 403

    async def test_a_post_from_another_site_is_refused(self, open_client: TestClient) -> None:
        resp = await open_client.post(
            "/chat", json={"message": "hi"}, headers={"Origin": "https://attacker.example"}
        )

        assert resp.status == 403

    async def test_a_client_that_is_not_a_browser_is_unaffected(
        self, open_client: TestClient
    ) -> None:
        # curl, scripts: loopback host, no Origin.
        resp = await open_client.post("/chat", json={"message": "hi"})

        assert resp.status == 200

    async def test_a_probe_under_any_name_is_answered(self, open_client: TestClient) -> None:
        # A load balancer asks under its own name; /health changes nothing.
        resp = await open_client.get("/health", headers={"Host": "lb.internal:8000"})

        assert resp.status == 200

    async def test_an_ip_address_is_accepted(self, open_client: TestClient) -> None:
        # A rebinding attack needs a name it controls; an address cannot be one.
        resp = await open_client.get("/actors", headers={"Host": "192.168.1.20:8000"})

        assert resp.status == 200


class TestAKeyedInstallIsNotHostChecked:
    async def test_a_scraper_on_the_container_network_is_answered(
        self, keyed_client: TestClient
    ) -> None:
        # Prometheus scrapes by service name; with a key, the key is the guard.
        resp = await keyed_client.get(
            "/metrics", headers={"Host": "wactorz-python:8000", "X-API-Key": API_KEY}
        )

        assert resp.status == 200
