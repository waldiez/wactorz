"""
Shared Google OAuth helper for CalendarAgent and GmailAgent.

Single token file covers both agents - one consent screen, both scopes granted.
The first time an agent needs credentials, this opens a local browser window
via the standard installed-app flow. Subsequent runs refresh silently.

Environment:
  GOOGLE_OAUTH_CLIENT_JSON   path to the OAuth client secret JSON downloaded
                             from Google Cloud Console (Desktop app type).
                             Default: ~/.wactorz/google_client.json
  GOOGLE_OAUTH_TOKEN_JSON    path where the refresh token is cached.
                             Default: ~/.wactorz/google_token.json
  GOOGLE_OAUTH_NONINTERACTIVE  if '1', refuse to open a browser and raise
                               instead. Use this on headless servers - run
                               the auth flow once locally, then copy the
                               token file to the server.

Scopes granted on first consent:
  https://www.googleapis.com/auth/calendar          (read/write events)
  https://www.googleapis.com/auth/gmail.modify      (read + send + label)

Adding scopes later requires deleting the token file and re-consenting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
]

_DEFAULT_DIR    = Path.home() / ".wactorz"
_CLIENT_JSON    = Path(os.getenv("GOOGLE_OAUTH_CLIENT_JSON", _DEFAULT_DIR / "google_client.json"))
_TOKEN_JSON     = Path(os.getenv("GOOGLE_OAUTH_TOKEN_JSON",  _DEFAULT_DIR / "google_token.json"))
_NONINTERACTIVE = os.getenv("GOOGLE_OAUTH_NONINTERACTIVE", "0").strip().lower() in {"1", "true", "yes"}

_creds_cache = None


class GoogleAuthError(RuntimeError):
    """Raised when OAuth credentials cannot be obtained."""


def _load_sync():
    """Synchronous credential load. Run inside an executor."""
    global _creds_cache

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GoogleAuthError(
            "Google API libraries not installed. Run: pip install wactorz[google-api]"
        ) from exc

    creds = _creds_cache

    if creds is None and _TOKEN_JSON.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(_TOKEN_JSON), SCOPES)
        except Exception as exc:
            logger.warning(f"Stored token unreadable ({exc}); will re-auth")
            creds = None

    if creds and creds.valid:
        _creds_cache = creds
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds)
            _creds_cache = creds
            return creds
        except Exception as exc:
            logger.warning(f"Token refresh failed ({exc}); will re-auth")
            creds = None

    if not _CLIENT_JSON.exists():
        raise GoogleAuthError(
            f"OAuth client secret not found at {_CLIENT_JSON}. "
            "Create one at https://console.cloud.google.com/apis/credentials "
            "(type 'Desktop app'), download the JSON, and set "
            "GOOGLE_OAUTH_CLIENT_JSON to its path."
        )

    if _NONINTERACTIVE:
        raise GoogleAuthError(
            "No valid token and GOOGLE_OAUTH_NONINTERACTIVE=1. "
            "Run the OAuth flow on a workstation with a browser, "
            f"then copy {_TOKEN_JSON} to this machine."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_JSON), SCOPES)
    creds = flow.run_local_server(port=0)
    _persist(creds)
    _creds_cache = creds
    return creds


def _persist(creds) -> None:
    _TOKEN_JSON.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_JSON.write_text(creds.to_json())
    try:
        os.chmod(_TOKEN_JSON, 0o600)
    except OSError:
        pass


async def get_credentials():
    """Return valid google.oauth2 Credentials, running OAuth if needed."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _load_sync)


async def build_service(api_name: str, version: str):
    """Build an authenticated googleapiclient service in a worker thread."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleAuthError(
            "google-api-python-client not installed. Run: pip install wactorz[google-api]"
        ) from exc

    creds = await get_credentials()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: build(api_name, version, credentials=creds, cache_discovery=False),
    )


def status() -> dict:
    """Quick status snapshot - useful for diagnostics."""
    return {
        "client_json":    str(_CLIENT_JSON),
        "token_json":     str(_TOKEN_JSON),
        "client_exists":  _CLIENT_JSON.exists(),
        "token_exists":   _TOKEN_JSON.exists(),
        "noninteractive": _NONINTERACTIVE,
        "scopes":         SCOPES,
    }
