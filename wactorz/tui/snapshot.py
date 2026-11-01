"""Snapshot of what the TUI renders."""

from dataclasses import dataclass, field


@dataclass
class HostStats:
    """The server's own vitals, as it last reported them."""

    cpu_pct: float = 0.0
    mem_used_mb: float = 0.0
    mem_total_mb: float = 0.0


@dataclass
class Snapshot:
    """One refresh tick of everything the TUI renders."""

    agents: list[dict] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    #: All the model spend the server has seen.
    cost_usd: float = 0.0
    #: The spend in the current period, which is what ``cost_limit_usd`` limits.
    period_spend_usd: float = 0.0
    cost_limit_usd: float = 0.0
    cost_period: str = ""
    #: Whether this terminal's connection to the server is up.
    connected: bool = False
    broker_connected: bool = False
    host: HostStats = field(default_factory=HostStats)

    @property
    def agent_count(self) -> int:
        """How many agents this tick saw."""
        return len(self.agents)

    @property
    def node_count(self) -> int:
        """How many nodes this tick saw."""
        return len(self.nodes)
