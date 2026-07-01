r"""Run-at-login toggle. The OS artifact is the single source of truth:

  macOS   ~/Library/LaunchAgents/<APP_ID>.plist
  Windows HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run  value "<APP_NAME>"
  Linux   ~/.config/autostart/<APP_ID>.desktop

is_enabled() reflects whether it exists; set_enabled() creates or removes it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from wactorz.desktop.config import APP_ID, APP_NAME, FROZEN

_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"
_DESKTOP = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_argv() -> list[str]:
    """Command that relaunches the app. Inside an AppImage, sys.executable is a
    transient mount path, so prefer $APPIMAGE (the stable .AppImage path). Then
    the frozen exe, else `python -m wactorz.desktop.app` from source.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage]
    if FROZEN:
        return [sys.executable]
    return [sys.executable, "-m", "wactorz.desktop.app"]


def is_enabled() -> bool:
    try:
        if sys.platform == "darwin":
            return _PLIST.exists()
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        return _DESKTOP.exists()
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Create or remove the autostart entry. Returns True on success."""
    try:
        if sys.platform == "darwin":
            _set_macos(enabled)
        elif os.name == "nt":
            _set_windows(enabled)
        else:
            _set_linux(enabled)
        return True
    except Exception:
        return False


def _set_macos(enabled: bool) -> None:
    if not enabled:
        _PLIST.unlink(missing_ok=True)
        return
    args = "\n".join(f"        <string>{escape(a)}</string>" for a in _launch_argv())
    _PLIST.parent.mkdir(parents=True, exist_ok=True)
    _PLIST.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{APP_ID}</string>\n"
        f"    <key>ProgramArguments</key>\n    <array>\n{args}\n    </array>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "</dict>\n</plist>\n"
    )


def _set_windows(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_WRITE) as key:
        if enabled:
            cmd = subprocess.list2cmdline(_launch_argv())
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def _find_installed_desktop() -> Path | None:
    """The installed app-menu launcher (<APP_ID>.desktop) in the XDG data dirs,
    if the app was installed (pip/system). None for an AppImage or source run.
    """
    name = f"{APP_ID}.desktop"
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for base in [home, *(d for d in data_dirs.split(":") if d)]:
        candidate = Path(base) / "applications" / name
        if candidate.is_file():
            return candidate
    return None


def _set_linux(enabled: bool) -> None:
    if not enabled:
        _DESKTOP.unlink(missing_ok=True)
        return
    _DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    installed = _find_installed_desktop()
    if installed is not None:
        # Copy the canonical launcher so Exec/Icon/StartupWMClass stay in sync
        # with packaging, then mark it as an autostart entry.
        content = installed.read_text()
        if "X-GNOME-Autostart-enabled" not in content:
            content = content.rstrip("\n") + "\nX-GNOME-Autostart-enabled=true\n"
        _DESKTOP.write_text(content)
        return
    # Fallback (AppImage / source run): synthesize a minimal entry. Exec resolves
    # via _launch_argv() ($APPIMAGE-aware); Icon matches the packaged launcher.
    _DESKTOP.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={shlex.join(_launch_argv())}\n"
        "Icon=wactorz\n"
        f"StartupWMClass={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
