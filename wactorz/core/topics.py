"""What can go into an MQTT topic this process publishes.

Two checks, for two moments. A topic is checked before it is queued, because
the publisher sends one message at a time, in order, and retries a failed one
first: a topic that can never be sent would hold up everything behind it. A name
is checked before an agent is given it, because the name becomes one level of
the agent's topics (``agents/by-name/<name>/task``), and every one of them would
be unsendable.

No imports, so both the publisher and the spawn code can use it without
reaching into ``core.mqtt``, which the publisher imports late to avoid a cycle.
"""

#: What paho refuses in a topic it is asked to publish: the two wildcards,
#: which only a subscription may use, and NUL, which MQTT forbids in any string.
FORBIDDEN_IN_TOPIC = ("+", "#", "\x00")

#: The longest topic MQTT can carry: its length travels as a two-byte field.
MAX_TOPIC_BYTES = 65535


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
    ``agents/by-name/+/task``, one level, so ``a/b`` never matches there.
    """
    if not name or not name.strip():
        return "the name is empty"
    bad = [repr(c) for c in FORBIDDEN_IN_TOPIC if c in name]
    if bad:
        return f"the name contains {' and '.join(bad)}, which cannot appear in an MQTT topic"
    return None
