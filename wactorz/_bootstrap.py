"""Process bootstrap: import path and platform fixups.

Importing this module prepares the interpreter to run Wactorz as an application.
The work happens at import time and is effectively idempotent: Python caches the
module, so the side effects apply exactly once regardless of how many entry
points import it.

Only what genuinely must happen at import lives here — the Windows event-loop
policy has to be set before any async library starts. Root logging is configured
by :func:`wactorz.monitoring.log_setup.setup_logging` instead.
"""

import asyncio
import io
import sys
from pathlib import Path

# Make the package importable when launched directly (not via the console script).
_PKG_DIR = str(Path(__file__).parent)
if sys.path[0] != _PKG_DIR:
    sys.path.insert(0, _PKG_DIR)

WACTORZ_BOOTSTRAP = False


def _bootstrap() -> None:
    """Handle windows event loop and encoding cases, setup logging."""
    global WACTORZ_BOOTSTRAP  # pylint: disable=global-statement
    if not WACTORZ_BOOTSTRAP:
        WACTORZ_BOOTSTRAP = True
        # Windows: MUST run before any async library is imported or started.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
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
