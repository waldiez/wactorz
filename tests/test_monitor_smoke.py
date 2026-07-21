"""End-to-end smoke test for the monitor's HTTP surface.

Builds the *real* app via ``build_app()`` and drives every read-only route with
a real aiohttp test client. The unit tests exercise handlers in isolation, which
leaves one gap: a handler whose body has a broken **function-local import**
still imports, still type-checks, and only fails when the route is actually
hit. That is exactly what the monitor split could break, so this test walks the
whole route table instead of asserting on payloads.

No MQTT broker and no actor registry are required — every handler must degrade
gracefully when ``runtime.registry`` is ``None`` (legacy/standalone mode).
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.monitor import runtime
from wactorz.monitor.app import build_app

# Routes that need a live actor/broker or mutate state — smoke-tested for
# reachability only, or skipped entirely (POST/DELETE are covered by the unit
# tests, which can set up the state they need).
_SKIP_PREFIXES = ("/ws",)


def _get_routes(app):
    """Every distinct GET path with no required path params."""
    seen = set()
    for route in app.router.routes():
        path = route.resource.canonical
        if route.method != "GET" or "{" in path or path in seen:
            continue
        if path.startswith(_SKIP_PREFIXES):
            continue
        seen.add(path)
    return sorted(seen)


@pytest.fixture
async def client():
    app = build_app()
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_every_get_route_responds(client):
    """No GET route may raise — a 5xx here means a handler blew up (e.g. a
    stale relative import after a module move)."""
    failures = []
    for path in _get_routes(client.app):
        resp = await client.get(path)
        if resp.status >= 500:
            failures.append(f"{path} -> {resp.status}\n{(await resp.text())[:400]}")
    assert not failures, "handlers raised:\n" + "\n".join(failures)


async def test_core_api_payloads(client):
    """The endpoints the dashboard needs on first paint return usable JSON."""
    assert (await client.get("/health")).status == 200

    actors = await client.get("/api/actors")
    assert actors.status == 200
    assert isinstance(await actors.json(), list)

    config = await client.get("/api/config")
    assert config.status == 200
    cfg = await config.json()
    assert "ws_url" in cfg
    assert "token" not in str(cfg).lower(), "no secret may reach the browser"

    feed = await client.get("/api/feed")
    assert feed.status == 200

    cost = await client.get("/api/cost")
    assert cost.status == 200


async def test_actor_routes_with_unknown_id(client):
    """Parametrised actor routes must 404/4xx cleanly, not crash, when the
    registry is empty."""
    for path in (
        "/api/actors/nope",
        "/api/actors/nope/metrics",
        "/api/actors/nope/history",
    ):
        resp = await client.get(path)
        assert resp.status < 500, f"{path} -> {resp.status}"


async def test_registry_none_is_the_default(client):
    """Guard the assumption this module rests on: these routes are exercised
    with no registry injected, which is the standalone/legacy path."""
    assert runtime.registry is None
