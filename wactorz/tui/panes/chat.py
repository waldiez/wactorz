"""Chat tab — a conversation transcript with the orchestrator (and @agent targets).

Each turn is its own ``Static``; the in-flight reply is updated in place as
chunks stream in, then left as the final message. User and agent text is shown
as a Rich ``Text`` (not markup) so stray brackets in content can't break the
render. The app feeds this pane via :meth:`add_user` / :meth:`begin_reply` /
:meth:`stream` / :meth:`end_reply`; routing lives in :class:`TUIContext`.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..context import Snapshot, TUIContext
from ..theme import NODE


class ChatPane(VerticalScroll):
    """Scrolling transcript with in-place streaming of the current reply."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reply: Static | None = None
        self._label = "main"
        self._buffer = ""

    def on_mount(self) -> None:
        """Seed the transcript with a one-line usage hint."""
        self.mount(
            Static(
                "[dim]Type below and press Enter. Prefix with @name to target one "
                "agent; everything else goes to the orchestrator.[/]",
                classes="chat-hint",
            )
        )

    def refresh_view(self, _snap: Snapshot, _ctx: TUIContext, /) -> None:
        """No-op: chat is event-driven. Present so the pane satisfies TUIView."""

    # ── Transcript updates (driven by the app's chat worker) ─────────────────

    def add_user(self, text: str) -> None:
        """Append the user's message to the transcript."""
        line = Text()
        line.append("you  ", style=f"bold {NODE}")
        line.append(text)
        self.mount(Static(line, classes="chat-user"))
        self.scroll_end(animate=False)

    def begin_reply(self, label: str = "main") -> Static:
        """Open an in-flight reply bubble attributed to ``label``, and return it."""
        self._label = label
        self._buffer = ""
        reply = Static(self._compose_line("…", body_dim=True), classes="chat-reply")
        self._reply = reply
        self.mount(reply)
        self.scroll_end(animate=False)
        return reply

    def stream(self, chunk: str) -> None:
        """Append a chunk to the in-flight reply, updating it in place."""
        # A chunk can arrive without begin_reply() when a caller streams cold.
        reply = self._reply if self._reply is not None else self.begin_reply()
        self._buffer += chunk
        reply.update(self._compose_line(self._buffer))
        self.scroll_end(animate=False)

    def end_reply(self) -> None:
        """Close the in-flight reply, marking it empty if nothing arrived."""
        if self._reply is not None and not self._buffer:
            self._reply.update(self._compose_line("(no response)", body_dim=True))
        self._reply = None
        self._buffer = ""

    # ── Rendering ────────────────────────────────────────────────────────────

    def _compose_line(self, body: str, *, body_dim: bool = False) -> Text:
        line = Text()
        line.append(f"{self._label}  ", style="bold green")
        line.append(body, style="dim" if body_dim else "")
        return line
