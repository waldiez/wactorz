"""One-time interactive OAuth login for the Google Calendar / Gmail agents.

Run once per machine to mint the OAuth token the agents reuse. It opens a browser
for consent and stores the token under ``~/.wactorz/`` (path is configurable via
``*_TOKEN_FILE``). Uses a direct Google OAuth flow with the scopes Wactorz needs
(notably *without* ``gmail.metadata``, which would block free-text mail search).

Usage::

    wactorz-google-login            # both, whichever is configured
    wactorz-google-login calendar
    wactorz-google-login gmail
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from ..core.integrations.gmail_mcp import GmailMcpClient
from ..core.integrations.google_calendar_mcp import GoogleCalendarMcpClient


async def _login_one(label: str, client, configured: bool) -> bool:
    if not configured:
        print(f"{label}: skipped — no {label.upper()}_MCP_CLIENT_ID/SECRET set.")
        return True
    print(f"\n=== {label} login ===")
    print("A browser window will open for consent. If it does not, open the printed URL.")
    result = await client.authorize_direct()
    print(result)
    return "authorized" in result.lower()


async def _run(service: str) -> int:
    ok = True
    if service in ("calendar", "both"):
        cal = GoogleCalendarMcpClient()
        ok &= await _login_one(
            "Calendar", cal, bool(cal.config.client_id() and cal.config.client_secret())
        )
    if service in ("gmail", "both"):
        gm = GmailMcpClient()
        ok &= await _login_one(
            "Gmail", gm, bool(gm.config.client_id() and gm.config.client_secret())
        )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for ``wactorz-google-login``."""
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="wactorz-google-login",
        description="One-time OAuth login for the Google Calendar and Gmail agents.",
    )
    parser.add_argument(
        "service",
        nargs="?",
        default="both",
        choices=["calendar", "gmail", "both"],
        help="Which agent to authorize (default: both configured).",
    )
    args = parser.parse_args(argv)
    sys.exit(asyncio.run(_run(args.service)))


if __name__ == "__main__":
    main()
