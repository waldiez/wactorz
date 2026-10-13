"""How long a generated agent's work takes, and how it ends.

Two calls carry everything a generated agent does: ``handle_task``, once per
task it is sent, and ``process()``, once per cycle of its loop. Each is timed
here, by agent, and a task is counted by how it ended.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`, because a node runs the same agents and serves none. What a node's
agents took reaches main through each agent's metrics frame instead, as the
percentiles `RecentDurations` keeps.
"""

import math
from collections import deque

from prometheus_client import Counter, Histogram

#: How a task ended: answered, raised, or still running when its time ran out.
COMPLETED = "completed"
FAILED = "failed"
TIMED_OUT = "timed_out"

#: Upper bounds in seconds, from a lookup that answers at once up to the
#: timeouts, a minute for a task and two for a cycle.
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

TASK_DURATION = Histogram(
    "wactorz_agent_task_duration_seconds",
    "Time a generated agent's handle_task took, by agent and by how it ended.",
    labelnames=("agent", "outcome"),
    buckets=_BUCKETS,
    registry=None,
)
PROCESS_DURATION = Histogram(
    "wactorz_agent_process_duration_seconds",
    "Time one cycle of a generated agent's process() took, returned or raised.",
    labelnames=("agent",),
    buckets=_BUCKETS,
    registry=None,
)
PROCESS_TIMEOUTS = Counter(
    "wactorz_agent_process_timeouts",
    "Cycles of a generated agent's process() that were still running when their time ran out.",
    labelnames=("agent",),
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (TASK_DURATION, PROCESS_DURATION, PROCESS_TIMEOUTS)

_OUTCOMES = (COMPLETED, FAILED, TIMED_OUT)


def forget(agent: str) -> None:
    """Drop ``agent``'s series, when it stops.

    Agent names can be model-authored and one-off, so series kept for every
    agent ever run would grow for the life of the process. Dropped, they
    cover the agents that are running, as the per-actor metrics do. One that
    starts again under the same name begins its series afresh, which
    Prometheus reads as a counter reset.
    """
    for outcome in _OUTCOMES:
        _remove(TASK_DURATION, agent, outcome)
    _remove(PROCESS_DURATION, agent)
    _remove(PROCESS_TIMEOUTS, agent)


def _remove(metric: Histogram | Counter, *labels: str) -> None:
    """Remove one label set, which may never have been recorded."""
    try:
        metric.remove(*labels)
    except KeyError:
        pass


#: How many recent durations an agent keeps for its percentiles.
WINDOW = 100


class RecentDurations:
    """The last `WINDOW` durations of one kind of call, for its p50 and p95."""

    def __init__(self) -> None:
        self._seconds: deque[float] = deque(maxlen=WINDOW)

    def add(self, seconds: float) -> None:
        """Remember one call's duration."""
        self._seconds.append(seconds)

    def summary(self, prefix: str) -> dict[str, float]:
        """``{prefix}_p50_s`` and ``{prefix}_p95_s`` over the window; empty before any call."""
        if not self._seconds:
            return {}
        ordered = sorted(self._seconds)
        return {
            f"{prefix}_p50_s": round(_nearest_rank(ordered, 50), 4),
            f"{prefix}_p95_s": round(_nearest_rank(ordered, 95), 4),
        }


def _nearest_rank(ordered: list[float], percent: int) -> float:
    """The value at ``percent`` in ``ordered``, by the nearest-rank method."""
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]
