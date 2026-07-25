"""Home tab — wordmark, session/runtime identity, and live quick-stats."""

from __future__ import annotations

import os

from textual.widgets import Static

from ..context import Snapshot, TUIContext
from ..format import human_bytes, human_duration
from ..logo import pick_logo
from ..meta import SUBTITLE, VERSION
from ..theme import INDIGO, NODE, SIGNATURE, VIOLET

_KEYBOARD_HELP = (
    "  [yellow]Keyboard[/]",
    "    Tab / Shift+Tab   switch tabs",
    "    /<tab>            jump to a tab (e.g. /agents)",
    "    Esc               focus the command bar",
    "    /quit             exit",
)


def state_dir() -> str:
    """Where the backend keeps its state, as the running process sees it."""
    return os.environ.get("WACTORZ_STATE_DIR", "./state")


def _identity_lines(snap: Snapshot, ctx: TUIContext) -> list[str]:
    """Who this session is, and what it is attached to."""
    broker = "[green]● connected[/]" if snap.broker_connected else "[red]○ offline[/]"
    return [
        f"  [b]Session[/]   [green]{ctx.user}[/]@[{VIOLET}]{ctx.host}[/]",
        f"  [b]Model[/]     [{NODE}]{ctx.llm_provider}[/] / [{NODE}]{ctx.llm_model}[/]",
        f"  [b]Broker[/]    {ctx.broker}  {broker}",
        f"  [b]State[/]     [magenta]{state_dir()}[/]",
    ]


def _stats_lines(snap: Snapshot) -> list[str]:
    """Live counters — the same numbers the status bar summarises."""
    host = snap.host
    cost_limit = f" / ${snap.cost_limit_usd:.2f}" if snap.cost_limit_usd > 0 else ""
    ram = f"{host.ram_used_gb:.1f} / {host.ram_total_gb:.1f} GiB"
    return [
        "  [yellow]Quick stats[/]",
        f"    [b]Agents[/]   [{NODE}]{snap.agent_count}[/]",
        f"    [b]Nodes[/]    [{NODE}]{snap.node_count}[/]",
        f"    [b]Cost[/]     [green]${snap.cost_usd:.2f}[/]{cost_limit}",
        f"    [b]CPU[/]      [{NODE}]{host.cpu_pct:.1f}%[/]  ({host.cpu_cores} cores)",
        f"    [b]RAM[/]      [{VIOLET}]{ram}[/]",
        f"    [b]Net[/]      [green]↓{human_bytes(host.net_down_bps)}[/]  "
        f"[yellow]↑{human_bytes(host.net_up_bps)}[/]",
        f"    [b]Uptime[/]   {human_duration(host.uptime_s)}",
    ]


class HomePane(Static):
    """A self-rendering landing panel. Width-aware (picks the logo variant)."""

    def refresh_view(self, snap: Snapshot, ctx: TUIContext, /) -> None:
        """Redraw the whole panel from the latest snapshot."""
        logo = pick_logo(max(self.size.width - 4, 20))
        self.update(
            "\n".join(
                [
                    f"[{INDIGO}]{logo}[/]",
                    f"  [dim]{SUBTITLE}[/]  [{SIGNATURE}]v{VERSION}[/]",
                    "",
                    *_identity_lines(snap, ctx),
                    "",
                    *_stats_lines(snap),
                    "",
                    *_KEYBOARD_HELP,
                ]
            )
        )
