"""Route Python logging into the TUI's logs pane.

When the TUI owns the terminal, log records written to stdout would corrupt the
Textual display. This bridge:
  * captures root-logger records into a bounded buffer the app drains, and
  * temporarily detaches console (stdout/stderr) stream handlers so the spam
    lands in the logs pane instead of on top of the UI.

It edits nothing on disk: handlers are swapped at runtime and fully restored on
:meth:`uninstall`. File handlers (e.g. wactorz.log) are left untouched.
"""

from __future__ import annotations

import logging
import sys
from collections import deque


def _writes_to_terminal(handler: logging.Handler) -> bool:
    """True if this stream handler targets the real terminal.

    Checked via isatty() rather than identity against sys.stdout: Textual swaps
    sys.stdout for its own writer at startup, so a handler created earlier (e.g.
    cli.py's console handler) still points at the original TTY and would no
    longer match sys.stdout — yet it's exactly the one that corrupts the UI.
    """
    stream = getattr(handler, "stream", None)
    if stream is None:
        return True
    if stream in (sys.stdout, sys.stderr):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        # No isatty (an exotic stream), or the stream is already closed.
        return False


class _BufferHandler(logging.Handler):
    """Appends formatted (levelno, line) tuples to a shared deque."""

    def __init__(self, buffer: deque, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._buffer = buffer
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer the formatted record; a bad log call must not crash the UI."""
        try:
            self._buffer.append((record.levelno, self.format(record)))
        except (TypeError, ValueError):
            # Mismatched %-args or an unformattable value: report through
            # logging's own channel rather than propagating into the caller.
            self.handleError(record)


class LogBridge:
    """Installs/removes the buffer handler and mutes console handlers."""

    def __init__(self, maxlen: int = 5000, level: int = logging.INFO) -> None:
        self.buffer: deque = deque(maxlen=maxlen)
        self._level = level
        self._handler = _BufferHandler(self.buffer, level)
        self._muted: list[logging.Handler] = []
        self._prev_root_level: int | None = None
        self._installed = False

    def install(self) -> None:
        """Capture root-logger records and mute the console handlers."""
        if self._installed:
            return
        root = logging.getLogger()
        # The root logger defaults to WARNING, which would drop INFO before it
        # ever reaches a handler. Lower it so the captured level is meaningful;
        # restored on uninstall.
        if root.level == 0 or root.level > self._level:
            self._prev_root_level = root.level
            root.setLevel(self._level)
        for h in list(root.handlers):
            # FileHandler subclasses StreamHandler — keep those (wactorz.log).
            if (
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and _writes_to_terminal(h)
            ):
                root.removeHandler(h)
                self._muted.append(h)
        root.addHandler(self._handler)
        self._installed = True

    def uninstall(self) -> None:
        """Restore the console handlers and the root level. Idempotent."""
        if not self._installed:
            return
        root = logging.getLogger()
        root.removeHandler(self._handler)  # no-op when already detached
        for h in self._muted:
            root.addHandler(h)
        self._muted.clear()
        if self._prev_root_level is not None:
            root.setLevel(self._prev_root_level)
            self._prev_root_level = None
        self._installed = False
