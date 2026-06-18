"""Linux desktop integration: install the app's .desktop + icon into the user's
XDG dirs on launch.

Gives the app a proper menu entry and taskbar icon, lets xdg-desktop-portal
resolve the app id (silences the "App info not found" QDBus warning), and gives
the autostart copy-the-launcher path (see autostart.py) a real file to copy.
Idempotent — rewritten each launch so Exec/$APPIMAGE stays current.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from wactorz.desktop.config import _ASSETS, APP_ID, APP_NAME, FROZEN

_APPS = Path.home() / ".local" / "share" / "applications"
_ICON_DIR = Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps"
_DESKTOP = _APPS / f"{APP_ID}.desktop"
_ICON = _ICON_DIR / f"{APP_ID}.png"


def _exec() -> str:
    """How the menu entry relaunches the app: the stable AppImage path, the
    frozen exe, or `python -m wactorz.desktop.app` from source."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    if FROZEN:
        return sys.executable
    return f"{sys.executable} -m wactorz.desktop.app"


def install() -> None:
    """Install ~/.local/share/applications/<APP_ID>.desktop (+ icon). Linux only,
    best-effort."""
    if not sys.platform.startswith("linux"):
        return
    try:
        # The bundled icon lives at a transient AppImage mount path, so copy it to
        # a stable location and reference it by theme name.
        src_icon = _ASSETS / "icon.png"
        have_icon = False
        if src_icon.exists():
            _ICON_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_icon, _ICON)
            have_icon = True
        _APPS.mkdir(parents=True, exist_ok=True)
        _DESKTOP.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Spawn, coordinate and monitor AI agents at runtime\n"
            f"Exec={_exec()}\n"
            f"Icon={APP_ID if have_icon else ''}\n"
            f"StartupWMClass={APP_ID}\n"
            "Categories=Utility;\n"
            "Terminal=false\n"
        )
    except Exception:
        pass
