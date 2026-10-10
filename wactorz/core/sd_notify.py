"""Telling systemd this process is alive, when systemd asked to be told.

A unit with ``WatchdogSec=`` set expects a message at least that often and
restarts the service when they stop. Sent from the event loop, the message says
more than "the process exists": it says the loop is running, which is what a
process stuck in blocking code no longer does while still looking alive.

Nothing here does anything unless systemd started the process with a
notification socket, so it is safe to call anywhere.
"""

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)


def notify(state: str) -> bool:
    """Send ``state`` to systemd. False when there is nobody to send it to."""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    # A leading @ names a socket in the abstract namespace, spelled with a NUL.
    target = "\0" + address[1:] if address.startswith("@") else address
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), target)
    except OSError as exc:
        logger.debug("[sd-notify] Could not send %r: %s", state, exc)
        return False
    return True


def watchdog_interval() -> float | None:
    """How often systemd expects to hear from this process, in seconds, or None.

    None when no watchdog is set for it, or when the one that is set is for
    another process: the variables are inherited by children.
    """
    raw = os.environ.get("WATCHDOG_USEC", "")
    if not raw.isdigit() or int(raw) <= 0:
        return None
    pid = os.environ.get("WATCHDOG_PID", "")
    if pid and pid != str(os.getpid()):
        return None
    return int(raw) / 1_000_000


async def watchdog_loop() -> None:
    """Tell systemd the event loop is running, for as long as it is.

    At half the interval systemd allows, so one message lost or late is not a
    restart. Returns at once when no watchdog is set.
    """
    interval = watchdog_interval()
    if interval is None:
        return
    logger.info("[sd-notify] Answering systemd's watchdog every %.0fs.", interval / 2)
    while True:
        notify("WATCHDOG=1")
        await asyncio.sleep(interval / 2)
