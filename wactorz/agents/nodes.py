"""What main knows about remote nodes, and the questions it answers about them.

A node reports itself by heartbeat: who it is, which agents it is running, and
some system metrics. Everything here reads that one dict — whether a node counts
as up, which ones do, what to show, and which node is running a given agent.

Freshness is one constant rather than a number written at each call site. The
four readers have to agree: a node that the listing calls online while the
migration check calls it gone is a node nothing can be done with, and the
disagreement would only appear at the boundary.

Deliberately knows nothing about MQTT, the registry or the actor system. It is a
dict of heartbeats with questions attached, so it can be tested as one.
"""

from __future__ import annotations

import time
from typing import Any

#: How recently a node must have reported to count as online.
#:
#: Short on purpose: this drives the indicator a person is looking at, so a node
#: that has gone away should say so quickly. It is not the window for acting on
#: a node's absence — deciding its agents are gone waits considerably longer, so
#: a brief network gap costs a grey dot rather than a deletion.
ONLINE_WINDOW_S = 30.0


class NodeManager:
    """The known remote nodes, keyed by name.

    Each entry is what the node last sent: ``last_seen``, ``agents``, and
    whatever system metrics it included. Entries are written by the heartbeat
    listener and read from here.
    """

    def __init__(self) -> None:
        self.known: dict[str, dict[str, Any]] = {}

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every known node, with its age resolved to an `online` flag.

        The key names are a contract beyond this module: the dashboard reads
        them off the wire, so a renamed or dropped key is invisible from here
        and visible there.
        """
        now = time.time()
        return [
            {
                "node": name,
                "agents": info.get("agents", []),
                "last_seen": info.get("last_seen", 0),
                "online": self._is_fresh(info, now),
                "pid": info.get("pid"),
                "uptime_s": info.get("uptime_s"),
                "cpu_pct": info.get("cpu_pct"),
                "mem_used_mb": info.get("mem_used_mb"),
                "mem_free_mb": info.get("mem_free_mb"),
            }
            for name, info in self.known.items()
        ]

    def is_online(self, node_name: str) -> bool:
        """Whether `node_name` has reported inside the window.

        A node nobody has heard of is offline rather than an error: callers ask
        about names that came from a user or a stale registry entry.
        """
        info = self.known.get(node_name)
        return bool(info) and self._is_fresh(info, time.time())

    def online_names(self) -> list[str]:
        """The online nodes, sorted — this reaches a person in an error message."""
        return sorted(name for name in self.known if self.is_online(name))

    def running_agent(self, name: str) -> str:
        """The online node running `name`, or "" if none currently claims it.

        Empty string rather than None because callers test it as a string. A
        node outside the window does not claim its agents, which is what lets a
        migration proceed rather than refusing on behalf of a node that is gone.
        """
        now = time.time()
        for node_name, info in self.known.items():
            if self._is_fresh(info, now) and name in info.get("agents", []):
                return node_name
        return ""

    def running_agents(self) -> set[str]:
        """Every agent name an online node claims to be running.

        A node outside the window contributes nothing, so an agent counts as
        remote only while something is still reporting it.
        """
        now = time.time()
        return {
            agent
            for info in self.known.values()
            if self._is_fresh(info, now)
            for agent in info.get("agents", [])
        }

    @staticmethod
    def _is_fresh(info: dict[str, Any], now: float) -> bool:
        """Whether a heartbeat is recent enough to count.

        Missing `last_seen` reads as 0, which is 1970 and always stale — the
        honest answer for a node that has never reported.
        """
        return (now - info.get("last_seen", 0)) < ONLINE_WINDOW_S
