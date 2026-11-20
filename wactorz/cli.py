# SPDX-License-Identifier: Apache-2.0
"""The ``wactorz`` command: ``python -m wactorz``, ``python -m wactorz.cli``, ``wactorz``.

Parses the arguments (:mod:`wactorz.arguments`) and launches the process. System
assembly and the run loop live in :mod:`wactorz.app`; the dev reloader in
:mod:`wactorz.dev_reload`; import-time setup in :mod:`wactorz._bootstrap`. This
module holds the command alone and the library imports none of it, so
``python -m`` finds it not yet loaded.
"""

import argparse
import asyncio
import logging
import sys

import wactorz._bootstrap  # noqa: F401  side effect: Windows event-loop + console encoding  # pyright: ignore[reportUnusedImport]
from wactorz.arguments import get_args


def _run_as_node(args: argparse.Namespace) -> None:
    """Run as an edge node, with logging a node's operator can read.

    The console alone rather than the server's log setup: a node writes to its
    journal or to `~/wactorz/<name>.log`, has no monitor to forward to, and
    should say something the moment it starts rather than after the app's
    startup sequence would have configured logging.
    """
    # Imported here rather than at the top, for the same reason `main` imports
    # `wactorz.app` here: both pull in the whole framework, and `--help` should
    # not have to wait for it.
    from wactorz.node import cli as node_cli

    node_cli.configure_logging(args)
    node_cli.run(args)


def main() -> None:
    """Start the app, or this process's node runner when ``--node`` names one."""
    args = get_args()
    # `is not None`, so a bare `--node` counts: it means node mode with the name
    # taken from the environment.
    if args.node is not None or args.name:
        _run_as_node(args)
        return

    from wactorz.app import app
    from wactorz.errors import StartupError

    refused: StartupError | None = None
    try:
        asyncio.run(app(args))
    except StartupError as exc:
        refused = exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A signal shuts down by cancelling the app task, which unwinds through
        # its own `finally` — the actors are already stopped by the time the
        # cancellation surfaces here. Swallow it so an intentional stop exits 0
        # and silently, rather than printing a traceback and reporting failure
        # to whatever supervises the process.
        pass
    if refused is not None:
        # Said once, as the last line, without a traceback: the message is the
        # whole report. Then the status a supervisor reads as "do not simply
        # restart me": the configuration has to change first.
        logging.getLogger(__name__).error("[startup] %s", refused)
        sys.exit(1)


if __name__ == "__main__":
    main()
