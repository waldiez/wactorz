"""Logs tab — the server's log stream with level filter, pause, clear and search.

The app hands it the server's entries with :meth:`LogsPane.add`, and forwards the
F2/F3/F4 keys and the matching ``/`` commands to the public methods here.
"""

import logging
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, RichLog

from ..format import LEVEL_CYCLE, log_line

_MAX_RECORDS = 5000


def record_of(entry: Mapping[str, Any]) -> tuple[int, str]:
    """A server log entry as ``(level, line)``, the line named as a console would."""
    level = str(entry.get("level") or "INFO")
    levelno = logging.getLevelName(level)
    stamp = time.strftime("%H:%M:%S", time.localtime(float(entry.get("ts") or 0.0)))
    line = f"{stamp} {level:<7} {entry.get('origin') or ''}: {entry.get('text') or ''}"
    return (levelno if isinstance(levelno, int) else logging.INFO), line


class LogsPane(Vertical):
    """Toolbar + scrolling log view. Owns the capture buffer and filter state."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Retained history so filter/level changes can re-render past lines.
        self.records: deque[tuple[int, str]] = deque(maxlen=_MAX_RECORDS)
        self.min_level = logging.INFO
        self.paused = False
        self.search = ""

    # ── Layout / lifecycle ───────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Toolbar (level/pause/clear/filter) above the scrolling log view."""
        with Horizontal(id="log-toolbar"):
            yield Button("level: INFO  F2", id="log-level", compact=True)
            yield Button("pause  F3", id="log-pause", compact=True)
            yield Button("clear  F4", id="log-clear", compact=True)
            yield Input(placeholder="filter text…", id="log-search")
        yield RichLog(
            id="logbox",
            markup=False,
            highlight=False,
            wrap=False,
            auto_scroll=True,
        )

    # ── Capture / filtering ──────────────────────────────────────────────────

    def _matches(self, levelno: int, line: str) -> bool:
        if levelno < self.min_level:
            return False
        return self.search in line.lower() if self.search else True

    def add(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Take in log entries as the server gives them."""
        logbox = self.query_one("#logbox", RichLog)
        for entry in entries:
            record = record_of(entry)
            self.records.append(record)
            if not self.paused and self._matches(*record):
                logbox.write(log_line(*record))

    def _rerender(self) -> None:
        logbox = self.query_one("#logbox", RichLog)
        logbox.clear()
        for levelno, line in self.records:
            if self._matches(levelno, line):
                logbox.write(log_line(levelno, line))

    def _update_buttons(self) -> None:
        lvl = logging.getLevelName(self.min_level)
        self.query_one("#log-level", Button).label = f"level: {lvl}  F2"
        pause = "resume" if self.paused else "pause"
        self.query_one("#log-pause", Button).label = f"{pause}  F3"

    # ── Public controls (shared by buttons, F-keys and / commands) ───────────

    def cycle_level(self) -> None:
        """Advance the minimum level through INFO → WARNING → ERROR."""
        i = LEVEL_CYCLE.index(self.min_level)
        self.min_level = LEVEL_CYCLE[(i + 1) % len(LEVEL_CYCLE)]
        self._update_buttons()
        self._rerender()

    def toggle_pause(self) -> None:
        """Stop/resume writing new lines; resuming re-renders what was missed."""
        self.paused = not self.paused
        self._update_buttons()
        if not self.paused:
            self._rerender()

    def clear_logs(self) -> None:
        """Drop the retained history and empty the view."""
        self.records.clear()
        self.query_one("#logbox", RichLog).clear()

    def set_filter(self, term: str) -> None:
        """Apply a case-insensitive substring filter and re-render."""
        self.query_one("#log-search", Input).value = term
        self.search = term.strip().lower()
        self._rerender()

    # ── Local events ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the toolbar buttons to the same methods the F-keys use."""
        bid = event.button.id
        if bid is None:
            return
        handler = {
            "log-level": self.cycle_level,
            "log-pause": self.toggle_pause,
            "log-clear": self.clear_logs,
        }.get(bid)
        if handler:
            handler()
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the user types in the search box."""
        if event.input.id == "log-search":
            self.search = event.value.strip().lower()
            self._rerender()
            event.stop()
