"""How Home Assistant's WebSocket API answers.

Every Home Assistant feature here goes through one WebSocket client: the
registry and state reads that map a home, and the service calls that move a
light. A slow or failing Home Assistant shows as slow or failing agents, with
nothing pointing at the cause; these say where the time went.

Requests are labelled by command type, which callers name in code (the registry
lists, ``get_states``, ``call_service``), so the set is small and fixed.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, the way the other module-level metrics are.
"""

from prometheus_client import Counter, Histogram

#: How a request ended: answered, answered with a failure, or not answered in time.
OK = "ok"
ERROR = "error"
TIMEOUT = "timeout"

#: Upper bounds in seconds, from a service call answered at once to a full
#: registry dump from a large installation on modest hardware.
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

REQUESTS = Counter(
    "wactorz_ha_requests",
    "Home Assistant WebSocket requests, by command and by how they ended.",
    labelnames=("command", "outcome"),
    registry=None,
)
REQUEST_DURATION = Histogram(
    "wactorz_ha_request_duration_seconds",
    "Time from a Home Assistant WebSocket request to its answer, or to giving up on it.",
    labelnames=("command",),
    buckets=_BUCKETS,
    registry=None,
)
CONNECT_DURATION = Histogram(
    "wactorz_ha_connect_duration_seconds",
    "Time to open a WebSocket to Home Assistant and authenticate on it.",
    buckets=_BUCKETS,
    registry=None,
)
CONNECT_FAILURES = Counter(
    "wactorz_ha_connect_failures",
    "Attempts to open an authenticated WebSocket to Home Assistant that failed.",
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (REQUESTS, REQUEST_DURATION, CONNECT_DURATION, CONNECT_FAILURES)


def record_request(command: str, outcome: str, seconds: float) -> None:
    """Count one finished request and the time it took."""
    REQUESTS.labels(command=command, outcome=outcome).inc()
    REQUEST_DURATION.labels(command=command).observe(seconds)
