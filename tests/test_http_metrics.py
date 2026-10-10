"""Both HTTP servers in the process count and time what they are asked.

The REST interface serves `/metrics`; the dashboard's server carries the chat
and its WebSocket. They record into the same metrics, told apart by `server`,
so the dashboard's traffic is on `/metrics` too. A WebSocket is counted and not
timed, since its handler lasts as long as the connection.
"""

import dataclasses
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

from tests.waiting import until
from wactorz import config
from wactorz.monitoring import http_metrics
from wactorz.monitoring.prometheus import PrometheusMonitor
from wactorz.web import runtime
from wactorz.web.app import build_app

#: A key a scraper presents as `Authorization: Bearer`, as Prometheus does.
KEY = "s3cret"


def _value(name: str, **labels: str) -> float:
    """One sample of the HTTP metrics, 0 when it has not been recorded."""
    registry = CollectorRegistry()
    for collector in http_metrics.COLLECTORS:
        registry.register(collector)
    return registry.get_sample_value(name, labels) or 0.0


@pytest.fixture(name="dashboard")
async def dashboard_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheDashboardServer:
    async def test_a_request_is_counted_and_timed_under_its_route(
        self, dashboard: TestClient
    ) -> None:
        labels = {"server": "dashboard", "method": "GET", "route": "/health"}
        before = _value("wactorz_http_requests_total", **labels)
        timed = _value("wactorz_http_request_duration_seconds_count", **labels)

        response = await dashboard.get("/health")

        assert response.status == 200
        assert _value("wactorz_http_requests_total", **labels) == before + 1
        assert _value("wactorz_http_request_duration_seconds_count", **labels) == timed + 1
        assert _value("wactorz_http_responses_total", **labels, status="200") >= 1

    async def test_a_path_never_becomes_a_label(self, dashboard: TestClient) -> None:
        # The path is caller-supplied: as a label, every new one would be a
        # new series. Routes come from the routing table, which here ends in a
        # catch-all for the single-page app.
        await dashboard.get("/no/such/path/a")
        await dashboard.get("/no/such/path/b")

        routes = {
            sample.labels.get("route", "")
            for metric in http_metrics.REQUESTS.collect()
            for sample in metric.samples
        }
        assert not [route for route in routes if "no/such" in route]

    async def test_a_dashboard_websocket_is_counted_while_it_is_open(
        self, dashboard: TestClient
    ) -> None:
        before = _value("wactorz_ws_connections")

        socket = await dashboard.ws_connect("/ws")
        await until(
            lambda: _value("wactorz_ws_connections") == before + 1,
            "the open connection to be counted",
        )
        await socket.close()
        await until(
            lambda: _value("wactorz_ws_connections") == before,
            "the closed connection to be gone from the count",
        )

    async def test_open_websockets_are_read_when_metrics_are(
        self, dashboard: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "ws_clients", {object(), object()})

        assert _value("wactorz_ws_connections") == 2


async def _ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for _ in ws:
        pass
    return ws


async def test_a_websocket_is_counted_and_not_timed() -> None:
    # Its handler returns when the connection closes; timed, one connection
    # would outweigh every request in the histogram.
    app = web.Application(middlewares=[http_metrics.middleware_for("probe")])
    app.router.add_get("/socket", _ws)
    labels = {"server": "probe", "method": "GET", "route": "/socket"}
    async with TestClient(TestServer(app)) as client:
        socket = await client.ws_connect("/socket")
        await socket.close()

    assert _value("wactorz_http_requests_total", **labels) == 1
    assert _value("wactorz_http_request_duration_seconds_count", **labels) == 0


async def test_the_rest_middleware_labels_its_requests_rest() -> None:
    app = web.Application(middlewares=[PrometheusMonitor.middleware])

    async def _ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/rest-probe", _ok)
    labels = {"server": "rest", "method": "GET", "route": "/rest-probe"}
    async with TestClient(TestServer(app)) as client:
        await client.get("/rest-probe")

    assert _value("wactorz_http_requests_total", **labels) == 1


class TestTheDashboardServesMetrics:
    """The REST interface runs only when chosen; the dashboard's server always does."""

    async def test_it_answers_with_the_metrics(self, dashboard: TestClient) -> None:
        response = await dashboard.get("/metrics")

        assert response.status == 200
        text = await response.text()
        assert "wactorz_actors_total" in text
        assert "wactorz_http_requests_total" in text

    async def test_with_a_key_it_asks_for_one(
        self, dashboard: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "CONFIG", dataclasses.replace(config.CONFIG, api_key=KEY))

        refused = await dashboard.get("/metrics")
        scraped = await dashboard.get("/metrics", headers={"Authorization": f"Bearer {KEY}"})

        assert refused.status == 401
        assert scraped.status == 200
