"""Wactorz TUI — a Textual app: tabbed monitor + chat over the actor system.

Launched in-process by ``wactorz --interface tui``
or standalone via ``python -m wactorz.tui``.
"""

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header


class WactorzTUI(App):
    """Top-level app."""

    BINDINGS = []  # TODO: add keyboard bindings

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield Footer()


def run() -> None:
    """Blocking entry point — run the TUI synchronously."""
    WactorzTUI().run()


async def run_async() -> None:
    """Async entry point — run the TUI on the current event loop."""
    await WactorzTUI().run_async()
