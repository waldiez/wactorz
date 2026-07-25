"""Logs tab — captured log stream with level filter, pause, clear and search.

Self-contained: it installs the :class:`LogBridge` on mount (routing logging
into this pane and muting terminal spam) and removes it on unmount, so the app
needs no knowledge of logging. The app forwards the F2/F3/F4 keys and the
matching ``/`` commands to the public methods here.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, RichLog

from ..format import LEVEL_CYCLE, log_line
from ..logbridge import LogBridge

_DRAIN_INTERVAL = 0.25
_MAX_RECORDS = 5000


class LogsPane(Vertical):
    """Toolbar + scrolling log view. Owns the capture buffer and filter state."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bridge = LogBridge(maxlen=_MAX_RECORDS)
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

    def on_mount(self) -> None:
        """Take over logging and start draining the capture buffer."""
        self.bridge.install()
        self.set_interval(_DRAIN_INTERVAL, self._drain)

    def on_unmount(self) -> None:
        """Hand logging back exactly as we found it."""
        self.bridge.uninstall()

    # ── Capture / filtering ──────────────────────────────────────────────────

    def _matches(self, levelno: int, line: str) -> bool:
        if levelno < self.min_level:
            return False
        return self.search in line.lower() if self.search else True

    def _drain(self) -> None:
        buf = self.bridge.buffer
        if not buf:
            return
        logbox = self.query_one("#logbox", RichLog)
        while buf:
            try:
                rec = buf.popleft()
            except IndexError:
                break
            self.records.append(rec)
            if not self.paused and self._matches(*rec):
                logbox.write(log_line(*rec))

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
