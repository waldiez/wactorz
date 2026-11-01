"""The TUI over a system started by this same process: `wactorz --interface tui`.

It connects over loopback like any other client. What this process adds is that
its own logging would land on top of the display, so console handlers are taken
off for as long as the TUI owns the terminal; what they would have said reaches
the logs tab through the server. File handlers are left alone.
"""

import asyncio
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..client import Client, UnavailableError
from .app import run_async
from .context import TUIContext

#: Seconds to wait for this process's monitor to start answering. Generous, as a
#: loaded machine is slow rather than broken, and how often to ask meanwhile.
MONITOR_PATIENCE_S = 60.0
MONITOR_POLL_S = 0.1


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


async def wait_for_monitor(client: Client, *, patience: float = MONITOR_PATIENCE_S) -> None:
    """Return once the monitor answers. It starts in the background, and takes a moment.

    Only a monitor that is not there yet is waited for: any other refusal is an
    answer, and raised. Raises the last failure if ``patience`` runs out.
    """
    deadline = time.monotonic() + patience
    while True:
        try:
            await client.server()
        except UnavailableError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(MONITOR_POLL_S)
        else:
            return


async def run_hosted(port: int, api_key: str) -> None:
    """Run the TUI against the monitor this process is serving on ``port``."""
    with console_quiet():
        async with Client(f"http://127.0.0.1:{port}", api_key=api_key) as client:
            await wait_for_monitor(client)
            await run_async(TUIContext(client, hosted=True))
