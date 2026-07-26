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

from collections.abc import AsyncGenerator
from typing import Any
from unittest import mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web import Application

from wactorz.web import runtime
from wactorz.web.app import build_app

# Routes that need a live actor/broker or mutate state — smoke-tested for
# reachability only, or skipped entirely (POST/DELETE are covered by the unit
# tests, which can set up the state they need).
_SKIP_PREFIXES = ("/ws",)

# A handler that blows up surfaces as 500 — aiohttp's unhandled-exception
# response. 503 is the opposite: routes for optional features return it
# deliberately when the extra is not installed (``/api/tts`` without
# ``wactorz[tts]``, so the dashboard falls back to the Web Speech API). On a
# machine without the extras that is a working route, not a broken one.
_FEATURE_UNAVAILABLE = 503


def _get_routes(app: Application) -> list[str]:
    """Every distinct GET path with no required path params."""
    seen = set()
    for route in app.router.routes():
        assert route.resource
        path = route.resource.canonical
        if route.method != "GET" or "{" in path or path in seen:
            continue
        if path.startswith(_SKIP_PREFIXES):
            continue
        seen.add(path)
    return sorted(seen)


@pytest.fixture(name="client")
async def client_fixture() -> AsyncGenerator[TestClient, Any]:
    """The real app, with network-backed optional features pinned off.

    ``/api/tts`` genuinely synthesizes speech through Microsoft's edge-tts
    service when ``wactorz[tts]`` is installed — which the ``all`` extra CI
    installs. Walking the route table must not depend on the network, so pin
    the feature to its not-installed state; the route is still dispatched, it
    just takes the 503 branch.
    """
    # Resolved here rather than at import time: tests/ext and
    # test_actor_decorators drop ext modules from sys.modules, and the ext
    # loader re-imports them — a module-level binding would go stale and pin
    # the wrong object. Started/stopped by hand so it holds for the whole
    # request regardless of fixture teardown order.
    from wactorz.ext import tts as tts_ext

    pinned = mock.patch.object(tts_ext._tts_state, "available", False)
    pinned.start()
    try:
        app = build_app()
        async with TestClient(TestServer(app)) as c:
            yield c
    finally:
        pinned.stop()


async def test_every_get_route_responds(client: TestClient) -> None:
    """No GET route may raise — a 500 here means a handler blew up (e.g. a
    stale relative import after a module move)."""
    failures = []
    for path in _get_routes(client.app):
        resp = await client.get(path)
        if resp.status >= 500 and resp.status != _FEATURE_UNAVAILABLE:
            failures.append(f"{path} -> {resp.status}\n{(await resp.text())[:400]}")
    assert not failures, "handlers raised:\n" + "\n".join(failures)


async def test_an_uninstalled_optional_feature_degrades_instead_of_crashing(client):
    """An optional feature that is not installed answers 503 with guidance.

    The client fixture pins TTS off, so this is the real dep-free behaviour
    regardless of what is installed on the machine running the suite.
    """
    resp = await client.get("/api/tts?text=hi")
    assert resp.status == _FEATURE_UNAVAILABLE
    assert "pip install" in await resp.text()


async def test_core_api_payloads(client: TestClient) -> None:
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


async def test_actor_routes_with_unknown_id(client: TestClient) -> None:
    """Parametrised actor routes must 404/4xx cleanly, not crash, when the
    registry is empty."""
    for path in (
        "/api/actors/nope",
        "/api/actors/nope/metrics",
        "/api/actors/nope/history",
    ):
        resp = await client.get(path)
        assert resp.status < 500, f"{path} -> {resp.status}"


async def test_registry_none_is_the_default(client: TestClient) -> None:
    """Guard the assumption this module rests on: these routes are exercised
    with no registry injected, which is the standalone/legacy path."""
    assert runtime.registry is None
