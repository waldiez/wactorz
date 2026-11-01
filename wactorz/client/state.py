"""The server's live state, folded from the socket so a reader need not ask for it.

Every consumer of `Client.events` that shows the system wants the same thing: the
newest agents and nodes, the totals, and whether anything is connected. Each
frame says only what changed, and a key it leaves out keeps its value: a missing
total is not a zero.
"""

from dataclasses import dataclass, field
from typing import Any

from .frames import Disconnected, Event, Frame, Reconnected

#: The topic the server's own vitals arrive on, inside a ``server_event`` frame.
HOST_TOPIC = "system/host"


@dataclass
class LiveState:
    """What is known now. Feed it every event with `apply`."""

    agents: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_messages: int = 0
    #: Whether this client's socket is up.
    connected: bool = False
    #: Whether the server is connected to its broker.
    broker_connected: bool = False
    #: The server's latest vitals: ``cpu``, ``mem_used_mb``, ``mem_total_mb``.
    host: dict[str, Any] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        """Take in one event from `Client.events`."""
        if isinstance(event, Disconnected):
            self.connected = False
        elif isinstance(event, Reconnected):
            self.connected = True
        elif isinstance(event, Frame):
            self._apply_frame(event)

    def _apply_frame(self, frame: Frame) -> None:
        self.connected = True
        raw = frame.raw
        state = raw.get("state")
        if isinstance(state, dict):
            self._apply_state(state)
        if frame.type == "mqtt_status":
            self.broker_connected = bool(raw.get("connected"))
        elif frame.type == "server_event" and raw.get("topic") == HOST_TOPIC:
            payload = raw.get("payload")
            if isinstance(payload, dict):
                self.host = payload

    def _apply_state(self, state: dict[str, Any]) -> None:
        for key in ("agents", "nodes", "alerts"):
            if isinstance(state.get(key), list):
                setattr(self, key, state[key])
        if "total_cost_usd" in state:
            self.total_cost_usd = float(state["total_cost_usd"] or 0.0)
        if "total_messages" in state:
            self.total_messages = int(state["total_messages"] or 0)
