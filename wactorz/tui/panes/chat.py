"""Chat tab — a conversation transcript with the orchestrator (and @agent targets).

Each turn is its own ``Static``; the in-flight reply is updated in place as
chunks stream in, then left as the final message. User and agent text is shown
as a Rich ``Text`` (not markup) so stray brackets in content can't break the
render. The app feeds this pane via :meth:`add_user` / :meth:`begin_reply` /
:meth:`stream` / :meth:`end_reply`, and :meth:`restore` puts the stored
conversation back when the app starts; routing lives in :class:`TUIContext`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..attachments import describe
from ..context import Snapshot, TUIContext
from ..theme import NODE

_HINT = (
    "[dim]Type below and press Enter. Prefix with @name to target one agent; "
    "everything else goes to the orchestrator. Drop a file onto the terminal to "
    "attach it.[/]"
)


class ChatPane(VerticalScroll):
    """Scrolling transcript with in-place streaming of the current reply."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reply: Static | None = None
        self._label = "main"
        self._buffer = ""

    def compose(self) -> ComposeResult:
        """The usage hint, first in the transcript so restored turns can follow it."""
        yield Static(_HINT, classes="chat-hint")

    def refresh_view(self, _snap: Snapshot, _ctx: TUIContext, /) -> None:
        """No-op: chat is event-driven. Present so the pane satisfies TUIView."""

    # ── Transcript updates (driven by the app's chat worker) ─────────────────

    def restore(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Put the stored conversation back, above anything said since the app started.

        Mounted after the hint rather than at the end: the history is read off the
        event loop, and a message sent before it arrives belongs below it.
        """
        if not rows:
            return
        turns = [_stored_turn(row) for row in rows]
        turns.append(
            Static(f"── earlier conversation · {len(rows)} messages ──", classes="chat-divider")
        )
        self.mount(*turns, after=self.query_one(".chat-hint"))
        self.scroll_end(animate=False)

    def add_user(self, text: str, attachments: Iterable[Mapping[str, Any]] = ()) -> None:
        """Append the user's message, and what was attached to it, to the transcript."""
        self.mount(Static(_user_line(text, attachments), classes="chat-user"))
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
        return _reply_line(self._label, body, body_dim=body_dim)


def _stored_turn(row: Mapping[str, Any]) -> Static:
    """One chat_log row as the transcript shows it."""
    content = str(row.get("content") or "")
    if row.get("role") == "user":
        files = row.get("attachments")
        listed = files if isinstance(files, list) else []
        return Static(_user_line(content, listed), classes="chat-user restored")
    label = str(row.get("agent_name") or "main")
    return Static(_reply_line(label, content), classes="chat-reply restored")


def _user_line(text: str, attachments: Iterable[object]) -> Text:
    line = Text()
    line.append("you  ", style=f"bold {NODE}")
    line.append(text)
    for item in attachments:
        if isinstance(item, Mapping):
            line.append(f"\n     {describe(item)}", style="dim")
    return line


def _reply_line(label: str, body: str, *, body_dim: bool = False) -> Text:
    line = Text()
    line.append(f"{label}  ", style="bold green")
    line.append(body, style="dim" if body_dim else "")
    return line
