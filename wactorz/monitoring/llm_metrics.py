"""How many requests reach each LLM provider, how they end, and how long they take.

Recorded where every provider is called from, `LLMProvider`'s public methods,
so a request counts once however many attempts the retry policy made of it:
what an operator needs to know is whether the people and agents asking got an
answer, and how long they waited for it.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, because the provider layer runs in processes that serve none -- a
node -- and must not need one to exist.
"""

from prometheus_client import Counter, Histogram

#: How a request ended: answered, the provider unavailable after every retry,
#: or any other failure -- a refused request, a bad key, a bug.
OK = "ok"
UNAVAILABLE = "unavailable"
ERROR = "error"

#: Upper bounds in seconds. A completion takes seconds, and a long one with
#: tools or reasoning minutes, so the default buckets, which stop at ten
#: seconds, would put most of them in one.
_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)

REQUESTS = Counter(
    "wactorz_llm_requests",
    "LLM requests, by provider and by how they ended.",
    labelnames=("provider", "outcome"),
    registry=None,
)
DURATION = Histogram(
    "wactorz_llm_request_duration_seconds",
    "Time from an LLM request being made to its answer or its failure, retries included.",
    labelnames=("provider",),
    buckets=_BUCKETS,
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (REQUESTS, DURATION)


def record(provider: str, outcome: str, seconds: float) -> None:
    """Count one finished request to ``provider`` and the time it took."""
    REQUESTS.labels(provider=provider, outcome=outcome).inc()
    DURATION.labels(provider=provider).observe(seconds)
