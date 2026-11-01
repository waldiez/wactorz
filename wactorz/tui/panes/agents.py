"""Agents tab — a live table of the server's agents and their vitals."""

from textual.widgets import DataTable

from ..context import Snapshot, TUIContext
from ..format import state_text

_COLUMNS = ("agent", "state", "task", "msgs", "cost", "node", "")


class AgentsPane(DataTable):
    """DataTable of agents, rebuilt each refresh (cursor position preserved)."""

    def on_mount(self) -> None:
        """Install the column headers once, before the first refresh."""
        self.add_columns(*_COLUMNS)

    def refresh_view(self, snap: Snapshot, _ctx: TUIContext, /) -> None:
        """Rebuild the table from the snapshot, keeping the cursor in place."""
        cursor = self.cursor_row
        self.clear()  # columns are kept
        for a in sorted(snap.agents, key=lambda x: x.get("name", "")):
            self.add_row(
                a.get("name", "?"),
                state_text(str(a.get("state", "?"))),
                str(a.get("task") or "idle"),
                str(a.get("messages_processed") or 0),
                f"${float(a.get('cost_usd') or 0.0):.4f}",
                a.get("node") or "local",
                "🔒" if a.get("protected") else "",
            )
        if self.row_count:
            self.move_cursor(row=min(cursor, self.row_count - 1))
