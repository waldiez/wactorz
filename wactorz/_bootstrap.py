# SPDX-License-Identifier: Apache-2.0
"""Windows platform fixups, applied at import.

Imported first by the package (``wactorz/__init__.py``), so these run before
anything imports aiohttp or starts a loop, whichever command started Wactorz.
"""

import asyncio
import io
import os
import sys
from collections.abc import MutableMapping

WACTORZ_BOOTSTRAP = False


def avoid_loop_sendfile(environ: MutableMapping[str, str]) -> None:
    """Make aiohttp send files by its own writes rather than ``loop.sendfile``.

    The selector loop set below has no native sendfile on Windows, and the
    copy asyncio falls back to writes the file through one reused buffer: once
    the reader is slower than the disk, the bytes that go out are not the
    file's. A browser reading the dashboard's bundle is such a reader, so the
    page fails to parse on some loads; every other file served loses bytes the
    same way. aiohttp's own path writes a chunk and waits for it to drain before
    reading the next, which is safe.

    aiohttp reads ``AIOHTTP_NOSENDFILE`` when it is first imported, hence here.
    A value already set is the operator's and is kept.
    """
    environ.setdefault("AIOHTTP_NOSENDFILE", "1")


def _bootstrap() -> None:
    """Set the Windows event-loop policy and console encoding. No-op elsewhere."""
    global WACTORZ_BOOTSTRAP
    if not WACTORZ_BOOTSTRAP:
        WACTORZ_BOOTSTRAP = True
        # Windows: MUST run before any async library is imported or started.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            avoid_loop_sendfile(os.environ)
            # Force UTF-8 on the real Windows console only. Skip when stdio has been
            # replaced (pytest capture, test runners, etc.) since re-wrapping a
            # capture stream breaks the harness on Python 3.13.
            # pytest capture / pythonw replace stdio
            # with objects that have no .buffer.
            _need_wrap = (
                (getattr(sys.stdout, "encoding", "") or "").lower() != "utf-8"
                and hasattr(sys.stdout, "buffer")
                and hasattr(sys.stderr, "buffer")
            )
            if _need_wrap:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
                sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


_bootstrap()


__all__ = ["WACTORZ_BOOTSTRAP"]
