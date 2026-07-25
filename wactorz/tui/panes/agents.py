"""Agents tab — a live table of local actors and their vitals."""

from __future__ import annotations

from textual.widgets import DataTable

from ..context import Snapshot, TUIContext
from ..format import human_duration, state_text

_COLUMNS = ("agent", "state", "type", "msgs", "restarts", "uptime", "node", "")


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
                a.get("agent_type", "core"),
                str(a.get("messages_processed", 0)),
                str(a.get("restart_count", 0)),
                human_duration(a.get("uptime", 0) or 0),
                a.get("node") or "local",
                "🔒" if a.get("protected") else "",
            )
        if self.row_count:
            self.move_cursor(row=min(cursor, self.row_count - 1))
