"""Wactorz TUI — a Textual app: tabbed monitor + chat over the actor system.

Launched in-process by ``wactorz --interface tui``
or standalone via ``python -m wactorz.tui``.
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane


class WactorzTUI(App):
    """Top-level app."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        with TabbedContent(initial="home"):
            with TabPane("home", id="home"):
                yield Static("WACTORZ — home tab (coming soon)", id="home-body")
        yield Footer()


def run() -> None:
    """Blocking entry point — run the TUI synchronously."""
    WactorzTUI().run()


async def run_async() -> None:
    """Async entry point — run the TUI on the current event loop."""
    await WactorzTUI().run_async()
