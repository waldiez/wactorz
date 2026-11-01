"""Home tab — wordmark, session/runtime identity, and live quick-stats."""

from textual.widgets import Static

from ..context import Snapshot, TUIContext
from ..logo import pick_logo
from ..meta import SUBTITLE, VERSION
from ..theme import INDIGO, NODE, SIGNATURE, VIOLET

_KEYBOARD_HELP = (
    "  [yellow]Keyboard[/]",
    "    Tab / Shift+Tab   switch tabs",
    "    /<tab>            jump to a tab (e.g. /agents)",
    "    Esc               focus the command bar",
    "    Drop a file       attach it to the next message (/detach removes it)",
    "    /quit             exit",
)


def _identity_lines(snap: Snapshot, ctx: TUIContext) -> list[str]:
    """Who this session is, and what it is attached to."""
    server = "[green]● connected[/]" if snap.connected else "[red]○ reconnecting[/]"
    broker = "[green]● connected[/]" if snap.broker_connected else "[red]○ offline[/]"
    lifetime = (
        "started here: closing this stops the agents"
        if ctx.hosted
        else "attached: the agents keep running when this closes"
    )
    return [
        f"  [b]Session[/]   [green]{ctx.user}[/]@[{VIOLET}]{ctx.host}[/]",
        f"  [b]Server[/]    {ctx.address}  {server}",
        f"              [dim]{lifetime}[/]",
        f"  [b]Model[/]     [{NODE}]{ctx.llm_provider}[/] / [{NODE}]{ctx.llm_model}[/]",
        f"  [b]Broker[/]    {broker}",
    ]


def _stats_lines(snap: Snapshot) -> list[str]:
    """Live counters — the same numbers the status bar summarises."""
    host = snap.host
    period = (
        [
            f"    [b]{snap.cost_period.capitalize():<8}[/] [green]${snap.period_spend_usd:.2f}[/]"
            f" / ${snap.cost_limit_usd:.2f}"
        ]
        if snap.cost_limit_usd > 0
        else []
    )
    return [
        "  [yellow]Quick stats[/]",
        f"    [b]Agents[/]   [{NODE}]{snap.agent_count}[/]",
        f"    [b]Nodes[/]    [{NODE}]{snap.node_count}[/]",
        f"    [b]Cost[/]     [green]${snap.cost_usd:.2f}[/]",
        *period,
        f"    [b]CPU[/]      [{NODE}]{host.cpu_pct:.1f}%[/]",
        f"    [b]Memory[/]   [{VIOLET}]{host.mem_used_mb:.0f} / {host.mem_total_mb:.0f} MB[/]",
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
