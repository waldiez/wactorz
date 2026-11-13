# SPDX-License-Identifier: Apache-2.0
"""What can go into an MQTT topic this process publishes, and how Wactorz's own are spelled.

Two checks, for two moments. A topic is checked before it is queued, because
the publisher sends one message at a time, in order, and retries a failed one
first: a topic that can never be sent would hold up everything behind it. A name
is checked before an agent is given it, because the name becomes one level of
the agent's topics (``agents/at/<node>/<name>/task``), and every one of them would
be unsendable.

Every topic under a root Wactorz owns (`OWN_ROOTS`) is built by the helpers
here and read back by `own_topic_parts` or `owner_of`, never spelled out or
split by position where it is used, so that how a root is spelled is decided in
one place (`_root`). ``tests/test_own_topics.py`` refuses either elsewhere.

That spelling is where the install's namespace goes (``WACTORZ_NAMESPACE``):
with one set, ``agents/a1/status`` is ``<ns>/agents/a1/status``, and the world's
topics are untouched. Code given a topic from outside -- an agent program, the
dashboard -- goes through `wire_topic` on the way in and `display_topic` on the
way out, so that it may keep writing the bare form.

The namespace is read once, at import, as every setting is; many topics here
and elsewhere are constants. Its only import is `wactorz.config`, which imports
nothing of Wactorz, so the publisher and the spawn code can use this module
without reaching into ``core.mqtt``, which the publisher imports late to avoid a
cycle.
"""

from ..config import NAMESPACE as _CONFIGURED_NAMESPACE

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


#: The roots of the topics Wactorz itself owns. Every topic under them is built
#: by the helpers below and read back by `own_topic_parts`, so that the one place
#: that spells a root (`_root`) decides what every such topic looks like.
#: ``shadow`` carries what a model on trial answers, in front of the topic the
#: served model publishes on (`shadow_topic`).
AGENTS = "agents"
NODES = "nodes"
MAIN = "main"
SYSTEM = "system"
MODELS = "models"
PIPELINES = "pipelines"
SHADOW = "shadow"
OWN_ROOTS = (AGENTS, NODES, MAIN, SYSTEM, MODELS, PIPELINES, SHADOW)

#: The roots of the topics that describe the world. They are shared by every
#: install on a broker and spelled by whoever publishes them, so a namespace
#: never goes in front of them. The broker's ``$SYS`` is not a root anyone may
#: name either.
WORLD_ROOTS = ("sensors", "home", "custom", "schedule", "homeassistant")

#: What a namespace may not contain: a level separator, the two wildcards, and
#: ``$``, which starts the broker's own topics.
FORBIDDEN_IN_NAMESPACE = ("/", "+", "#", "$")


def namespace_error(namespace: str) -> str | None:
    """Why ``namespace`` cannot be this install's namespace, or None if it can; "" is none.

    Stricter than an agent name: it is written into the broker's access list as
    it stands, one rule per line, so whitespace anywhere would break the rule.
    A root's own name is refused too, as it would read as that root.
    """
    if not namespace:
        return None
    if any(c.isspace() or not c.isprintable() for c in namespace):
        return f"WACTORZ_NAMESPACE={namespace!r} contains whitespace or a control character"
    bad = [repr(c) for c in FORBIDDEN_IN_NAMESPACE if c in namespace]
    if bad:
        return (
            f"WACTORZ_NAMESPACE={namespace!r} contains {' and '.join(bad)}; it is one topic level"
        )
    if namespace in OWN_ROOTS or namespace in WORLD_ROOTS:
        return f"WACTORZ_NAMESPACE={namespace!r} is the name of a topic root; choose another"
    return None


#: Why the configured namespace cannot be used, or None. Main and a node refuse
#: to start on it; until then the topics are built with no namespace, so that
#: importing a module never fails on a setting.
NAMESPACE_REFUSAL = namespace_error(_CONFIGURED_NAMESPACE)

#: This install's namespace, or "" for none.
NAMESPACE = "" if NAMESPACE_REFUSAL else _CONFIGURED_NAMESPACE


def _root(root: str) -> str:
    """The first levels of every topic under ``root``: the namespace, then the root."""
    return f"{NAMESPACE}/{root}" if NAMESPACE else root


def _topic(root: str, levels: tuple[str, ...]) -> str:
    return "/".join((_root(root), *levels))


def agent_topic(agent: str, *leaves: str) -> str:
    """``agents/<agent>/<leaves...>``; a wildcard is one more level (``agent_topic("+", "manifest")``)."""
    return _topic(AGENTS, (agent, *leaves))


def node_topic(node: str, *leaves: str) -> str:
    """``nodes/<node>/<leaves...>``; a wildcard is one more level (``node_topic("+", "heartbeat")``)."""
    return _topic(NODES, (node, *leaves))


def main_topic(*leaves: str) -> str:
    """``main/<leaves...>``, the topics main itself serves."""
    return _topic(MAIN, leaves)


def system_topic(*leaves: str) -> str:
    """``system/<leaves...>``, the install's own announcements (health, host)."""
    return _topic(SYSTEM, leaves)


def model_topic(name: str, *leaves: str) -> str:
    """``models/<name>/<leaves...>``, a model's versions and what is said about them."""
    return _topic(MODELS, (name, *leaves))


def pipeline_topic(name: str, *leaves: str) -> str:
    """``pipelines/<name>/<leaves...>``, what a pipeline publishes about itself (its tick)."""
    return _topic(PIPELINES, (name, *leaves))


def shadow_topic(topic: str) -> str:
    """``shadow/<topic>``: where a model on trial answers what the served one publishes on ``topic``.

    The topic inside stays bare, so a trial's answers read the same whatever the namespace.
    """
    return _topic(SHADOW, (display_topic(topic),))


def root_prefix(root: str) -> str:
    """What every topic under ``root`` starts with, slash included, for a prefix match."""
    return f"{_root(root)}/"


def own_topic_parts(topic: str) -> list[str] | None:
    """The levels of ``topic``, root first, if Wactorz owns it; None if it does not.

    The reading side of the helpers above: code that looks at a topic's levels
    by position goes through this rather than ``topic.split("/")``, so that the
    positions mean the same however a root is spelled.
    """
    for root in OWN_ROOTS:
        prefix = root_prefix(root)
        if topic.startswith(prefix):
            return [root, *topic[len(prefix) :].split("/")]
    return None


def owner_of(topic: str, root: str) -> str:
    """Whose ``topic`` under ``root`` is: the node or agent named in it, or "" if it is not under ``root``.

    ``owner_of("nodes/rpi/heartbeat", NODES)`` is ``"rpi"``.
    """
    parts = own_topic_parts(topic)
    if parts is None or parts[0] != root or len(parts) < 2:
        return ""
    return parts[1]


def every_namespace(root: str, *leaves: str) -> tuple[str, str]:
    """Two filters for ``<root>/<leaves...>`` under no namespace and under any one.

    The bare form and ``+/<root>/<leaves...>``. Between them they match the topic
    whatever namespace it was sent under, this install's included, and never
    the same message twice: they differ in depth. For hearing what is on the
    wrong namespace (main's heartbeat check); everything else listens under
    its own.
    """
    return ("/".join((root, *leaves)), "/".join(("+", root, *leaves)))


def namespace_of(topic: str, root: str) -> tuple[str, str] | None:
    """The namespace ``topic`` under ``root`` was sent under ("" for none), and whose it is.

    ``("lab", "rpi")`` for ``lab/nodes/rpi/heartbeat``, ``("", "rpi")`` for the bare
    form; None for a topic under ``root`` at neither depth. The reading side of
    `every_namespace`.
    """
    levels = topic.split("/")
    if len(levels) > 1 and levels[0] == root:
        return ("", levels[1])
    if len(levels) > 2 and levels[1] == root:
        return (levels[0], levels[2])
    return None


def wire_topic(topic: str) -> str:
    """``topic`` as it travels: a bare ``<own root>/...`` gains the namespace; anything else stays.

    For a topic given from outside, where whoever wrote it need not know the
    namespace: an agent program's publish, subscribe or contract, a topic the
    dashboard asks to follow. A world topic, a topic already prefixed and a
    filter that starts with a wildcard are left as they are; so is every topic
    when no namespace is set. Applying it twice changes nothing, since a
    namespace is never a root's name.
    """
    if not NAMESPACE:
        return topic
    head = topic.split("/", 1)[0]
    return f"{NAMESPACE}/{topic}" if head in OWN_ROOTS and "/" in topic else topic


def display_topic(topic: str) -> str:
    """``topic`` as shown to whoever gave it bare: ``<ns>/<own root>/...`` loses the namespace.

    The reverse of `wire_topic`, and only for Wactorz's own topics: a world topic
    whose first level happens to be the namespace (``house/temperature`` with
    ``house`` set) is somebody else's and stays as it is.
    """
    if NAMESPACE and own_topic_parts(topic) is not None:
        return topic[len(NAMESPACE) + 1 :]
    return topic


#: Where a task for an agent on a node goes: ``agents/at/<node>/<name>/task``.
#: Only that node subscribes, so a name other nodes also run is not a problem.
TASK_BY_NODE = agent_topic("at")

#: Where a task goes by the agent's name alone, ``agents/by-name/<name>/task``,
#: for a node from before tasks went by node. Every node subscribes to it, and
#: each runs its own agent of that name.
TASK_BY_NAME = agent_topic("by-name")


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
    parts = own_topic_parts(topic)
    if parts is None or parts[0] != AGENTS or parts[-1:] != ["task"]:
        return None
    if len(parts) == 5 and parts[1] == "at":
        return parts[3]
    if len(parts) == 4 and parts[1] == "by-name":
        return parts[2]
    return None


def is_task_route(topic: str) -> bool:
    """Whether ``topic`` is under either task family, a task or a reply to one."""
    return topic.startswith((f"{TASK_BY_NODE}/", f"{TASK_BY_NAME}/"))
