"""Which broker messages are forwarded to browsers.

The server subscribes to whole subtrees — `agents/#`, `system/#`, `nodes/#` and
the Home Assistant state-change topic — because agents publish freely within
them. The dashboard, though, consumes a fixed and knowable set. Forwarding the
remainder puts whatever any agent happens to publish onto every open browser's
socket, and that surface grows with each new agent rather than with this file.

The patterns mirror what the frontend's `ServerEventRouter` dispatches, the
feed-only suffixes its `raw` fallback maps in `agents/mapping.ts`, and the
Home Assistant subtree the fallback feeds. ⚠ **A topic added to the router or
the feed mappers needs adding here too, or it will be relayed and silently
dropped on arrival.**
State is unaffected: every message still reaches `parse_topic`, so an agent's
metrics update the dashboard whether or not the raw frame is echoed.

Matching the *subscribed* Home Assistant prefix is deliberate — a topic the
server never subscribed to cannot arrive, so there is nothing to relay.
"""

#: `agents/{id}/<leaf>` topics the dashboard acts on. The last two are consumed
#: not by `ServerEventRouter` but by the raw-fallback feed mappers in
#: `frontend/src/agents/mapping.ts` (actuation audit trail, anomaly alerts) —
#: the other place a topic addition must mirror.
_AGENT_LEAVES = frozenset(
    {
        "heartbeat",
        "status",
        "alert",
        "chat",
        "spawn",
        "metrics",
        "logs",
        "completed",
        "actuations",
        "anomaly",
    }
)

#: `system/*` topics, matched exactly — the subtree carries more than these.
_SYSTEM_TOPICS = frozenset({"system/qa-flag", "system/spawn", "system/health", "system/host"})

#: The only `nodes/{id}/<leaf>` the dashboard reads.
_NODE_LEAF = "heartbeat"

#: Home Assistant state changes, consumed wholesale by the activity feed. Kept as
#: a prefix because the bridge publishes one topic per entity beneath it.
_HA_PREFIX = "homeassistant/state_changes"


def is_relayed(topic: str) -> bool:
    """Whether a browser has any use for this topic's raw payload."""
    if topic in _SYSTEM_TOPICS:
        return True
    if topic == _HA_PREFIX or topic.startswith(f"{_HA_PREFIX}/"):
        return True
    parts = topic.split("/")
    if len(parts) != 3:
        return False
    root, _, leaf = parts
    if root == "agents":
        return leaf in _AGENT_LEAVES
    if root == "nodes":
        return leaf == _NODE_LEAF
    return False
