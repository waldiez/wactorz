"""Settings tab — read-only view of the running config, secrets masked.

Every value goes through :func:`~wactorz.tui.format.mask_secret` or
:func:`~wactorz.tui.format.plain`; nothing here may print a credential.
"""

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..context import Snapshot, TUIContext
from ..format import plain


def _row(label: str, value: str) -> str:
    """One aligned ``label  value`` line."""
    return f"    [b]{label:<14}[/] {value}"


def _llm_lines(snap: Snapshot, ctx: TUIContext) -> list[str]:
    """Provider, model and the spend cap."""
    limit = (
        f"[green]${snap.cost_limit_usd:.2f}[/] / {snap.cost_period}, "
        f"${snap.period_spend_usd:.2f} spent"
        if snap.cost_limit_usd > 0
        else "[dim]disabled[/]"
    )
    return [
        "  [yellow]LLM[/]",
        _row("provider", plain(ctx.llm_provider)),
        _row("model", plain(ctx.llm_model)),
        _row("cost limit", limit),
    ]


def _server_lines(ctx: TUIContext) -> list[str]:
    """What the server says about itself: only what it gives any client."""
    server = ctx.server
    home_assistant = (server.raw.get("ha") or {}).get("url") or ""
    return [
        "  [yellow]Server[/]",
        _row("address", plain(ctx.address)),
        _row("version", plain(server.version)),
        _row("uploads", "on" if server.uploads else "off"),
        _row("home assistant", plain(home_assistant)),
    ]


class SettingsPane(VerticalScroll):
    """Scrollable, grouped config readout. Reflects live config each refresh."""

    def compose(self) -> ComposeResult:
        """A single body Static, rewritten on every refresh."""
        yield Static(id="settings-body")

    def refresh_view(self, snap: Snapshot, ctx: TUIContext, /) -> None:
        """Re-render the readout from what the server reports."""
        body = "\n".join(
            [
                "  [dim]read-only — what the server reports[/]",
                "",
                *_llm_lines(snap, ctx),
                "",
                *_server_lines(ctx),
            ]
        )
        self.query_one("#settings-body", Static).update(body)
