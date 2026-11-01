"""The TUI over a system started by this same process: `wactorz --interface tui`.

It connects over loopback like any other client. What this process adds is that
its own logging would land on top of the display, so console handlers are taken
off for as long as the TUI owns the terminal; what they would have said reaches
the logs tab through the server. File handlers are left alone.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from ..client import Client
from .app import run_async
from .context import TUIContext


def _writes_to_terminal(handler: logging.Handler) -> bool:
    """True if this stream handler targets the real terminal.

    Checked with isatty() rather than identity against sys.stdout: Textual swaps
    sys.stdout for its own writer at startup, so a handler made earlier still
    points at the original terminal and would no longer match.
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


def _is_console(handler: logging.Handler) -> bool:
    # FileHandler subclasses StreamHandler: those keep wactorz.log going.
    return (
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and _writes_to_terminal(handler)
    )


@contextmanager
def console_quiet() -> Iterator[None]:
    """Take the console handlers off the root logger, and put them back after."""
    root = logging.getLogger()
    muted = [handler for handler in root.handlers if _is_console(handler)]
    for handler in muted:
        root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in muted:
            root.addHandler(handler)


async def run_hosted(port: int, api_key: str) -> None:
    """Run the TUI against the monitor this process is serving on ``port``."""
    with console_quiet():
        async with Client(f"http://127.0.0.1:{port}", api_key=api_key) as client:
            await run_async(TUIContext(client))
