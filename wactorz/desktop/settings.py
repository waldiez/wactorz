"""Desktop preferences, persisted to desktop_settings.json in the data dir.

Only preferences without an OS-level source of truth live here — currently the
auto-update-check toggle. (Autostart state lives in the OS artifact, not here.)
"""

from __future__ import annotations

import json

from wactorz.desktop.config import DATA_DIR

_SETTINGS_FILE = DATA_DIR / "desktop_settings.json"
_DEFAULTS = {"auto_update_check": False}


def _load() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            return {**_DEFAULTS, **json.loads(_SETTINGS_FILE.read_text())}
    except Exception:
        pass
    return dict(_DEFAULTS)


def _save(data: dict) -> None:
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def auto_update_check() -> bool:
    """Whether the shell looks for a newer release at launch (default off)."""
    return bool(_load().get("auto_update_check", False))


def set_auto_update_check(enabled: bool) -> None:
    """Persist the launch-time update-check preference."""
    data = _load()
    data["auto_update_check"] = bool(enabled)
    _save(data)
