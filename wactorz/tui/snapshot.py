"""Snapshot of what the TUI renders."""

from dataclasses import dataclass, field


@dataclass
class HostStats:
    """Machine vitals sampled once per refresh tick (psutil, when available)."""

    cpu_pct: float = 0.0
    cpu_cores: int = 0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    net_down_bps: float = 0.0
    net_up_bps: float = 0.0
    uptime_s: float = 0.0


@dataclass
class Snapshot:
    """One refresh tick of everything the TUI renders."""

    agents: list[dict] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    cost_limit_usd: float = 0.0
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
