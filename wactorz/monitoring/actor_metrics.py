"""How long an actor's messages wait in its mailbox, and how long it takes over them.

Every actor takes its messages one at a time, so a slow handler shows twice:
in its own time, and as the wait of every message queued behind it. Both are
timed per actor as each message is taken, leaving out the heartbeats, status
pings and stops the actor counts as noise.

The wait counts from when the message was made, wherever it waited: in the
mailbox, or with its sender for room in a full one. A high wait beside
``wactorz_actor_messages_refused_total`` is a full mailbox.

A handler that hands its work to a task of its own -- a generated agent's
``handle_task`` does, so its mailbox stays free for the replies it waits on --
is timed until it hands over; the work itself is in `agent_metrics`.

The metrics are created unregistered and handed to whichever registry serves
`/metrics`. An actor's series go when it stops, so they cover the actors that
are running, as the per-actor gauges read from the registry do.
"""

from prometheus_client import Histogram

#: Upper bounds in seconds, from a message taken at once to one queued behind
#: a handler that held the actor for minutes.
_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

QUEUE_WAIT = Histogram(
    "wactorz_actor_queue_wait_seconds",
    "Time a message waited in an actor's mailbox before the actor took it.",
    labelnames=("actor_name",),
    buckets=_BUCKETS,
    registry=None,
)
MESSAGE_DURATION = Histogram(
    "wactorz_actor_message_duration_seconds",
    "Time an actor's handler took over one message, from taking it to finishing.",
    labelnames=("actor_name",),
    buckets=_BUCKETS,
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (QUEUE_WAIT, MESSAGE_DURATION)


def forget(actor_name: str) -> None:
    """Drop ``actor_name``'s series, when it stops."""
    for metric in COLLECTORS:
        try:
            metric.remove(actor_name)
        except KeyError:
            pass
