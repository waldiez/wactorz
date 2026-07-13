"""Process bootstrap: import path, platform fixups, and root logging.

Importing this module prepares the interpreter to run Wactorz as an application.
The work happens at import time and is effectively idempotent: Python caches the
module, so the side effects apply exactly once regardless of how many entry
points import it.
"""

import asyncio
import io
import logging
import sys
from pathlib import Path

# Make the package importable when launched directly (not via the console script).
_PKG_DIR = str(Path(__file__).parent)
if sys.path[0] != _PKG_DIR:
    sys.path.insert(0, _PKG_DIR)

__WACTORZ_BOOTSTRAPPED__ = False


def _bootstrap() -> None:
    """Handle windows event loop and encoding cases."""
    global __WACTORZ_BOOTSTRAPPED__  # pylint: disable=global-statement
    if not __WACTORZ_BOOTSTRAPPED__:
        __WACTORZ_BOOTSTRAPPED__ = True
        # Windows: MUST run before any async library is imported or started.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # The default cp1252 console encoding mangles non-ASCII output; force UTF-8.
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler("wactorz.log", encoding="utf-8"),
            ],
        )


_bootstrap()


def bootstrap() -> None:
    """Handle windows event loop and encoding cases."""
    _bootstrap()


__all__ = ["bootstrap"]
