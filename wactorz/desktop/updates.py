"""Update check against the latest GitHub release.

A check notifies the user and, when a newer release is found, remembers it
(version + release-page URL) so the tray can offer a "Download" item. An
interactive (manual) check also opens the release page right away; a background
check stays quiet unless an update is found.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import webbrowser

from wactorz.desktop.config import APP_NAME
from wactorz.desktop.notifications import notify

_LATEST_RELEASE_URL = "https://api.github.com/repos/waldiez/wactorz/releases/latest"
_RELEASES_PAGE = "https://github.com/waldiez/wactorz/releases"

# Set once a newer release is seen: {"version": str, "url": str}. Drives the
# tray "Download vX" item; read lazily when the menu is shown.
_pending_update: dict | None = None


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version into ints for comparison; non-numeric parts drop."""
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a newer release than `current`. Zero-pads to equal
    length so e.g. 0.5 vs 0.5.0 compare equal rather than older."""
    a, b = _version_tuple(latest), _version_tuple(current)
    if not a:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def pending_version() -> str:
    """Version of a known newer release, or "" if none/not yet checked."""
    return _pending_update["version"] if _pending_update else ""


def open_download() -> None:
    """Open the release page for the pending update (or the releases index)."""
    url = _pending_update["url"] if _pending_update else _RELEASES_PAGE
    webbrowser.open(url)


def check_for_updates(interactive: bool = True) -> None:
    """Check for a newer release off the GUI thread. Interactive (the manual tray
    check) always reports a result and opens the release page when an update is
    found; a background check (interactive=False) stays silent unless one is."""
    threading.Thread(target=_update_check_task, args=(interactive,), daemon=True).start()


def _update_check_task(interactive: bool) -> None:
    global _pending_update
    try:
        from wactorz import __version__ as current

        req = urllib.request.Request(
            _LATEST_RELEASE_URL,
            headers={"User-Agent": "Wactorz-Desktop", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest = (data.get("tag_name") or "").lstrip("v")
        url = data.get("html_url") or _RELEASES_PAGE
        if latest and _is_newer(latest, current):
            _pending_update = {"version": latest, "url": url}
            notify(APP_NAME, f"Update available: v{latest} (you have v{current}).")
            if interactive:
                webbrowser.open(url)
        elif interactive:
            notify(APP_NAME, f"Wactorz is up to date (v{current}).")
    except Exception:
        if interactive:
            notify(APP_NAME, "Could not check for updates — see your connection and try again.")
