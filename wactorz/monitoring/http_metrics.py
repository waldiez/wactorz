"""What the HTTP servers in this process are asked, and how they answer.

Two servers run in one process: the REST interface, which also serves
`/metrics`, and the dashboard's, which carries the chat and its WebSocket. Both
record here, told apart by the `server` label, so the dashboard's traffic shows
on the same `/metrics` as the rest.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, the way the other module-level metrics are.
"""

import time

from aiohttp import web
from aiohttp.typedefs import Handler, Middleware
from prometheus_client import Counter, Gauge, Histogram

#: The `server` label of the REST interface and of the dashboard's server.
REST = "rest"
DASHBOARD = "dashboard"

#: Route label for requests that matched no route in the routing table.
UNMATCHED_ROUTE = "<unmatched>"

REQUESTS = Counter(
    "wactorz_http_requests",
    "HTTP requests received, by server, method and route.",
    labelnames=("server", "method", "route"),
    registry=None,
)
RESPONSES = Counter(
    "wactorz_http_responses",
    "HTTP responses returned, by server, method, route and status.",
    labelnames=("server", "method", "route", "status"),
    registry=None,
)
DURATION = Histogram(
    "wactorz_http_request_duration_seconds",
    "Time an HTTP request took to answer. WebSocket connections are not timed.",
    labelnames=("server", "method", "route"),
    registry=None,
)
WS_CONNECTIONS = Gauge(
    "wactorz_ws_connections",
    "Dashboard WebSocket connections open now.",
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (REQUESTS, RESPONSES, DURATION, WS_CONNECTIONS)


def route_label(request: web.Request) -> str:
    """Registered route pattern for a request, or a constant when none matched.

    The label must come from the routing table, never from the request line:
    an unrouted path is caller-supplied, so returning it would let anyone
    open a new time series per request and grow the metric without bound.
    """
    route = getattr(request.match_info, "route", None)
    resource = getattr(route, "resource", None)
    canonical = getattr(resource, "canonical", None)
    if canonical:
        return canonical
    return UNMATCHED_ROUTE


def is_websocket(request: web.Request) -> bool:
    """Whether ``request`` asks to become a WebSocket, whose handler lasts as long as it."""
    return request.headers.get("Upgrade", "").lower() == "websocket"


def middleware_for(server: str) -> Middleware:
    """An aiohttp middleware that counts and times requests to ``server``.

    A WebSocket upgrade is counted and not timed: its handler returns when the
    connection closes, hours later, and one would outweigh every request in
    the histogram. How many are open is `WS_CONNECTIONS`.
    """

    @web.middleware
    async def record(request: web.Request, handler: Handler) -> web.StreamResponse:
        route = route_label(request)
        method = request.method
        started = time.perf_counter()
        status = 500
        REQUESTS.labels(server=server, method=method, route=route).inc()
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            RESPONSES.labels(server=server, method=method, route=route, status=str(status)).inc()
            if not is_websocket(request):
                DURATION.labels(server=server, method=method, route=route).observe(
                    time.perf_counter() - started
                )

    return record
