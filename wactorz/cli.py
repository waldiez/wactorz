"""Command-line entry point: argument parsing and process launch.

System assembly and the run loop live in :mod:`wactorz.app`; the dev reloader in
:mod:`wactorz.dev_reload`; import-time setup in :mod:`wactorz._bootstrap`.
"""

import argparse
import asyncio
import logging
import os
import sys

# pylint: disable=unused-import
import wactorz._bootstrap  # noqa: F401  side effect: Windows event-loop + console encoding  # pyright: ignore[reportUnusedImport]
from wactorz.config import CONFIG


def get_args() -> argparse.Namespace:
    """Parse the cli args."""
    parser = argparse.ArgumentParser(description="Wactorz - Multi-Agent Framework")
    parser.add_argument("--interface", choices=["cli", "rest", "discord", "whatsapp", "telegram"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--llm", choices=["anthropic", "openai", "ollama", "nim", "gemini", "none"])
    parser.add_argument("--ollama-model", help="Ollama model name (e.g. llama3, mistral)")
    parser.add_argument(
        "--nim-model",
        help="NVIDIA NIM model, e.g. meta/llama-3.3-70b-instruct or deepseek-ai/deepseek-r1",
    )
    parser.add_argument(
        # No default, matching --ollama-model and --nim-model: argparse fills a
        # default in whether or not the flag was passed, so one here overrode
        # LLM_MODEL on every run and pinned Gemini to a single model for good.
        "--gemini-model",
        help="Google Gemini model (default: LLM_MODEL), e.g. gemini-3.6-flash",
    )
    # Tokens are accepted but deprecated as arguments. A process's argv is
    # world-readable on Linux — any local user running `ps` sees the value, and
    # it lands in shell history — so the environment variable is the supported
    # route. Kept working rather than removed: silently breaking an existing
    # launcher is worse than a warning it can act on.
    parser.add_argument("--discord-token", help=argparse.SUPPRESS)
    parser.add_argument("--mqtt-broker")
    parser.add_argument("--mqtt-port", type=int)
    parser.add_argument("--telegram-token", help=argparse.SUPPRESS)
    parser.add_argument("--telegram-allowed-user-id", type=int)
    parser.add_argument(
        "--monitor-port",
        type=int,
        default=int(os.getenv("MONITOR_PORT", str(CONFIG.ws_port))),
        help="Port for the background web UI / monitor server (default: 8888)",
    )
    parser.add_argument(
        "--no-monitor", action="store_true", help="Disable the background web UI server"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Watch wactorz/ for changes and auto-restart (dev mode)",
    )
    # ── Edge node ────────────────────────────────────────────────────────────
    # `--node <name>` runs this process as an edge node instead of as the
    # server: it connects to the broker, listens on nodes/<name>/… and runs the
    # agents main sends it. The same package either way, which is what a node
    # gets installed now — see wactorz/node/.
    parser.add_argument(
        "--node",
        metavar="NAME",
        # Optional value: `--node` alone takes the name from WACTORZ_NODE, which
        # `/deploy` writes into every node's `.env`. The flag is still what
        # chooses the role, though — a bare environment variable must not turn a
        # server into a node, and one can reach a server's environment.
        nargs="?",
        const="",
        help=(
            "Run as an edge node under this name instead of starting the server. "
            "With no name, $WACTORZ_NODE is used."
        ),
    )
    # The spellings the single-file runner took. Kept working, and hidden: a
    # node deployed before the package was installed there has a systemd unit
    # with these in its ExecStart, and it should survive the upgrade.
    parser.add_argument("--name", help=argparse.SUPPRESS)
    parser.add_argument("--broker", help=argparse.SUPPRESS)
    parser.add_argument("--loglevel", default="INFO", help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args()
    _warn_about_tokens_on_the_command_line(args)

    return args


def _warn_about_tokens_on_the_command_line(args: argparse.Namespace) -> None:
    """Say so when a secret was passed where every local user can read it.

    A process's argv is world-readable on Linux — `ps` shows it to anyone on the
    box, and the shell records it in history. The environment variable carries
    the same value without either. Warned rather than refused: an existing
    launcher should keep working, and it cannot act on a failure it never sees.
    """
    exposed = [
        flag
        for flag, value in (
            ("--discord-token", args.discord_token),
            ("--telegram-token", args.telegram_token),
        )
        if value
    ]
    for flag in exposed:
        env = flag.removeprefix("--").replace("-", "_").upper()
        print(
            f"warning: {flag} puts a secret in this process's command line, where any "
            f"local user can read it with `ps`. Set {env} in the environment instead.",
            file=sys.stderr,
        )


def _run_as_node(args: argparse.Namespace) -> None:
    """Run as an edge node, with logging a node's operator can read.

    ``basicConfig`` rather than the server's log setup: a node writes to its
    journal or to `~/wactorz/<name>.log`, has no monitor to forward to, and
    should say something the moment it starts rather than after the app's
    startup sequence would have configured logging.
    """
    # Imported here rather than at the top, for the same reason `main` imports
    # `wactorz.app` here: both pull in the whole framework, and `--help` should
    # not have to wait for it.
    from wactorz.node import cli as node_cli

    logging.basicConfig(
        level=getattr(logging, str(args.loglevel).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
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

    try:
        asyncio.run(app(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A signal shuts down by cancelling the app task, which unwinds through
        # its own `finally` — the actors are already stopped by the time the
        # cancellation surfaces here. Swallow it so an intentional stop exits 0
        # and silently, rather than printing a traceback and reporting failure
        # to whatever supervises the process.
        pass


if __name__ == "__main__":
    main()
