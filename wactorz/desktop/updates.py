"""Update check against the latest GitHub release."""
from __future__ import annotations

import json
import threading
import urllib.request

from wactorz.desktop.config import APP_NAME
from wactorz.desktop.notifications import notify

_LATEST_RELEASE_URL = "https://api.github.com/repos/waldiez/wactorz/releases/latest"


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


def check_for_updates(notify_if_current: bool = True) -> None:
    """Check for a newer release off the GUI thread. With notify_if_current False
    (the startup/auto check) it stays silent unless an update is found; the
    manual tray check leaves it True so the user always gets a result."""
    threading.Thread(target=_update_check_task, args=(notify_if_current,), daemon=True).start()


def _update_check_task(notify_if_current: bool) -> None:
    try:
        from wactorz import __version__ as current

        req = urllib.request.Request(
            _LATEST_RELEASE_URL,
            headers={"User-Agent": "Wactorz-Desktop", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latest = json.loads(resp.read().decode()).get("tag_name", "").lstrip("v")
        if latest and _is_newer(latest, current):
            notify(APP_NAME, f"Update available: v{latest} (you have v{current}).")
        elif notify_if_current:
            notify(APP_NAME, f"Wactorz is up to date (v{current}).")
    except Exception:
        if notify_if_current:
            notify(APP_NAME, "Could not check for updates — see your connection and try again.")
