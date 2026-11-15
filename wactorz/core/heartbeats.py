# SPDX-License-Identifier: Apache-2.0
"""The two unrelated shapes both called "heartbeat" in this tree.

A *node* heartbeat (`nodes/{node}/heartbeat`) says how one machine is doing;
an *agent* heartbeat (`agents/{actor_id}/heartbeat`) says how one agent is
doing. Different producer, different topic, different reader -- the node one
is read by :mod:`wactorz.agents.main.nodes`, the agent one by
:mod:`wactorz.web.events`. Nothing in common but the name.
"""

from typing import TypedDict


class NodeHeartbeat(TypedDict, total=False):
    """A node's own report of itself, sent every few seconds, not retained.

    `node.runner._node_heartbeat_loop` writes every key each time it runs; a
    runner too old to know a key simply never sends it (`version`, `runtime`,
    `signing`, `slow_retry`, `namespace`), which is why this is `total=False` rather than
    the resource readings needing it -- those are individually `| None`
    instead: `node.resources.read` always includes the key, with `None` for
    a reading this platform cannot take (never a guessed zero).

    What main keeps from this (`NodeManager.known[node]`) is a different,
    derived shape -- it adds `last_seen` and `signed`, computed on receipt,
    and drops `broker`/`agent_count`/`timestamp` -- and `list_nodes()` builds
    a third again for the dashboard. Both live in `agents.main.nodes`, not
    here, since neither travels on the wire.
    """

    node: str
    version: str
    runtime: str
    node_id: str
    timestamp: float
    agents: list[str]
    #: Sent, but derivable from ``len(agents)``; no reader uses it.
    agent_count: int
    #: Sent, but no reader was found for it.
    broker: str
    pid: int
    uptime_s: float
    cpu_pct: float | None
    mem_used_mb: float | None
    mem_free_mb: float | None
    swap_used_mb: float | None
    load_1m: float | None
    load_5m: float | None
    disk_free_mb: float | None
    temp_c: float | None
    #: Only a Raspberry Pi's firmware reports this; ``None`` elsewhere.
    throttled: list[str] | None
    signing: str
    signing_failures: int
    tls: bool
    slow_retry: list[str]
    #: The namespace the node runs under, "" for none. A node on another
    #: namespace than main's publishes where main does not listen, so main reads
    #: this only off the heartbeats it watches for that (`agents.main.nodes`).
    namespace: str


class AgentHeartbeat(TypedDict):
    """One agent's own report of itself, retained on its own heartbeat leaf.

    Written whole by `Actor._build_heartbeat`, every key always present from
    the one base producer -- so this is the one shape in this tree that can
    honestly be `total=True`. `web.events.record_heartbeat` still tests
    `essential`/`node`/`protected` with `"x" in data` rather than `.get("x")`,
    on purpose: a present-but-falsy value means something different from an
    absent key for *those three*, a holdover from when the key could be
    missing -- harmless now that it never is, but a refactor here must not
    assume it is therefore safe to switch to `.get()`. The in-process
    liveness check (`core.registry.Supervisor`, `monitoring.actor_metrics`)
    reads `actor.metrics.last_heartbeat` directly rather than this dict -- a
    second, separate path that happens to agree with it.
    """

    actor_id: str
    name: str
    timestamp: float
    state: str
    memory_mb: float
    #: What the agent is doing right now, e.g. "idle" -- unrelated to any one
    #: task's own id or text (see `wactorz.core.task_text.TaskEnvelope`).
    task: str
    protected: bool
    essential: bool
    node: str | None


class DynamicAgentHeartbeat(AgentHeartbeat):
    """What `DynamicAgent._build_heartbeat` adds to the base report."""

    code: str
    description: str
    agent_type: str


__all__ = ["AgentHeartbeat", "DynamicAgentHeartbeat", "NodeHeartbeat"]
