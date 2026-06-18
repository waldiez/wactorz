"""Shared constants and paths for the desktop shell."""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

APP_NAME = "Wactorz"
APP_ID = "io.waldiez.wactorz"          # desktop-file id / WM_CLASS
HOST = "127.0.0.1"


def _free_port() -> int:
    """An OS-assigned free loopback port, so the desktop's own backend never
    collides with another wactorz already holding the default 8888 (which would
    otherwise make the health probe attach to that instance's UI instead)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _resolve_port() -> int:
    """Honour a valid MONITOR_PORT; otherwise grab a free port dynamically."""
    raw = os.environ.get("MONITOR_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _free_port()


PORT = _resolve_port()
URL = f"http://{HOST}:{PORT}"
FROZEN = getattr(sys, "frozen", False)
ICON_EXT = {"win32": "ico", "darwin": "icns"}.get(sys.platform, "png")

# When frozen, the entry script's __file__ is not under the package, so resolve
# bundled assets from the PyInstaller extraction dir instead of relative to it.
_ASSETS = (
    Path(sys._MEIPASS) / "wactorz" / "desktop" / "assets"  # type: ignore[attr-defined]
    if FROZEN
    else Path(__file__).with_name("assets")
)
APP_ICON = _ASSETS / f"icon.{ICON_EXT}"

SPLASH_BG = "#0A0E1A"          # window surface colour while the page paints


def _data_dir() -> Path:
    """Per-user writable directory for backend state + logs."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "wactorz"


DATA_DIR = _data_dir()
# Desktop shell's capture of the backend child's stdout/stderr.
BACKEND_LOG = DATA_DIR / "desktop-backend.log"
# Last window geometry, restored on the next launch.
WINDOW_STATE_FILE = DATA_DIR / "window_state.json"
