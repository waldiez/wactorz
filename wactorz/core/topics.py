"""What can go into an MQTT topic this process publishes.

Two checks, for two moments. A topic is checked before it is queued, because
the publisher sends one message at a time, in order, and retries a failed one
first: a topic that can never be sent would hold up everything behind it. A name
is checked before an agent is given it, because the name becomes one level of
the agent's topics (``agents/at/<node>/<name>/task``), and every one of them would
be unsendable. And where a task for an agent on a node goes, spelled once.

No imports, so both the publisher and the spawn code can use it without
reaching into ``core.mqtt``, which the publisher imports late to avoid a cycle.
"""

#: What paho refuses in a topic it is asked to publish: the two wildcards,
#: which only a subscription may use, and NUL, which MQTT forbids in any string.
FORBIDDEN_IN_TOPIC = ("+", "#", "\x00")

#: The longest topic MQTT can carry: its length travels as a two-byte field.
MAX_TOPIC_BYTES = 65535

#: The ``agents/<id>/<leaf>`` topics an agent may leave retained, cleared when it
#: is deleted -- by main, by the server and by a node alike, so none of them
#: leaves a trace the others clear. ``questions`` is the agent's question book
#: (`wactorz.core.questions`).
RETAINED_AGENT_LEAVES = (
    "status",
    "heartbeat",
    "metrics",
    "logs",
    "spawned",
    "manifest",
    "errors",
    "detections",
    "results",
    "completed",
    "questions",
)


def publish_topic_error(topic: str) -> str | None:
    """Why ``topic`` cannot be published, or None if it can.

    Empty is refused too. MQTT v5 allows it only together with a topic alias,
    which nothing here sends; without one the broker drops the connection.
    """
    if not topic:
        return "the topic is empty"
    bad = [repr(c) for c in FORBIDDEN_IN_TOPIC if c in topic]
    if bad:
        return f"the topic contains {' and '.join(bad)}"
    try:
        size = len(topic.encode("utf-8"))
    except UnicodeEncodeError:
        return "the topic is not valid UTF-8"
    if size > MAX_TOPIC_BYTES:
        return f"the topic is longer than {MAX_TOPIC_BYTES} bytes"
    return None


def topic_name_error(name: str) -> str | None:
    """Why ``name`` cannot be one level of a topic, or None if it can.

    ``/`` is allowed. It makes a topic deeper rather than unsendable, and a local
    agent with it in its name still receives tasks, since it subscribes to its
    exact topic. A remote one does not: the runner subscribes to
    ``agents/at/<node>/+/task``, one level, so ``a/b`` never matches there.
    """
    if not name or not name.strip():
        return "the name is empty"
    bad = [repr(c) for c in FORBIDDEN_IN_TOPIC if c in name]
    if bad:
        return f"the name contains {' and '.join(bad)}, which cannot appear in an MQTT topic"
    return None


#: Where a task for an agent on a node goes: ``agents/at/<node>/<name>/task``.
#: Only that node subscribes, so a name other nodes also run is not a problem.
TASK_BY_NODE = "agents/at"

#: Where a task goes by the agent's name alone, ``agents/by-name/<name>/task``,
#: for a node from before tasks went by node. Every node subscribes to it, and
#: each runs its own agent of that name.
TASK_BY_NAME = "agents/by-name"


def task_topic(node: str, name: str) -> str:
    """The topic a task for ``name`` on ``node`` goes on."""
    return f"{TASK_BY_NODE}/{node}/{name}/task"


def task_by_name_topic(name: str) -> str:
    """The topic a task for ``name`` goes on, for a node that takes tasks only by name."""
    return f"{TASK_BY_NAME}/{name}/task"


def node_task_subscription(node: str) -> str:
    """What ``node`` subscribes to for the tasks sent to its agents."""
    return f"{TASK_BY_NODE}/{node}/+/task"


def reply_topic(node: str, name: str, key: str) -> str:
    """Where a reply to a task ``name`` on ``node`` sent comes back, by the sender's key."""
    return f"{TASK_BY_NODE}/{node}/{name}/reply/{key}"


def task_target(topic: str) -> str | None:
    """The name of the agent a task topic is for, or None for a topic that is no task."""
    parts = topic.split("/")
    if parts[-1:] != ["task"]:
        return None
    if len(parts) == 5 and "/".join(parts[:2]) == TASK_BY_NODE:
        return parts[3]
    if len(parts) == 4 and "/".join(parts[:2]) == TASK_BY_NAME:
        return parts[2]
    return None


def is_task_route(topic: str) -> bool:
    """Whether ``topic`` is under either task family, a task or a reply to one."""
    return topic.startswith((f"{TASK_BY_NODE}/", f"{TASK_BY_NAME}/"))
