"""Wactorz desktop shell: native window (PyWebView) + tray (pystray) wrapping
the Python backend.

One process. The backend runs as a child (`python -m wactorz`, or `<exe>
--run-backend` when frozen). The main thread owns the webview loop (required on
macOS). The tray runs detached on mac/Windows, in a daemon thread on Linux.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import webview
import pystray
from dotenv import load_dotenv, find_dotenv
from PIL import Image


load_dotenv(find_dotenv())

APP_NAME = "Wactorz"
HOST = "127.0.0.1"
PORT = int(os.environ.get("MONITOR_PORT", "8888"))
URL = f"http://{HOST}:{PORT}"
FROZEN = getattr(sys, "frozen", False)
ICON_EXT = "ico" if "win" in sys.platform else "png"
APP_ICON = Path(__file__).with_name("assets") / f"icon.{ICON_EXT}"

_backend: "subprocess.Popen | None" = None
_window = None
_hidden = False


# ── backend child ───────────────────────────────────────────────────────────
def _spawn_backend() -> "subprocess.Popen":
    env = dict(os.environ, MONITOR_PORT=str(PORT))
    cmd = [sys.executable, "--run-backend"] if FROZEN else [sys.executable, "-m", "wactorz"]
    return subprocess.Popen(cmd, env=env)


def run_backend() -> None:
    """Invoked as `<exe> --run-backend` (frozen) — equivalent to `python -m wactorz`."""
    import runpy
    sys.argv = ["wactorz"]
    runpy.run_module("wactorz", run_name="__main__")


def _wait_for_backend(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=1):
                return True
        except Exception:
            time.sleep(0.4)
    return False


# ── JS bridge (window.pywebview.api.*) ──────────────────────────────────────
class Api:
    def notify(self, title: str, body: str) -> None:
        _notify(title, body)


def _notify(title: str, body: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=body, app_name=APP_NAME)
    except Exception:
        pass


# ── tray ────────────────────────────────────────────────────────────────────
def _load_icon() -> Image.Image:
    if APP_ICON.exists():
        return Image.open(APP_ICON)
    return Image.new("RGBA", (64, 64), (90, 120, 255, 255))  # fallback swatch


def _toggle(icon=None, item=None) -> None:
    global _hidden
    if _window is None:
        return
    if _hidden:
        _window.show()
    else:
        _window.hide()
    _hidden = not _hidden


def _quit(icon, item) -> None:
    if _window is not None:
        _window.hide()
    icon.stop()
    _shutdown()


def _build_tray() -> pystray.Icon:
    menu = pystray.Menu(
        pystray.MenuItem("Show / Hide", _toggle, default=True),
        pystray.MenuItem("Quit Wactorz", _quit),
    )
    return pystray.Icon("wactorz", _load_icon(), APP_NAME, menu)


def _start_tray(icon: pystray.Icon) -> None:
    icon.run_detached()


# ── lifecycle ───────────────────────────────────────────────────────────────
def _shutdown(*_) -> None:
    global _backend
    if _backend and _backend.poll() is None:
        _backend.terminate()
        try:
            _backend.wait(timeout=5)
        except Exception:
            _backend.kill()
    os._exit(0)


def _on_closing() -> bool:
    """Window 'X' hides to tray instead of quitting; quit is tray-only."""
    global _hidden
    if _window:
        _window.hide()
        _hidden = True
    return False  # cancel the real close


def launch_desktop() -> None:
    global _backend, _window
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _backend = _spawn_backend()
    if not _wait_for_backend():
        _notify(APP_NAME, "Backend did not start in time")

    _window = webview.create_window(
        APP_NAME, URL, width=1200, height=700, min_size=(900, 600), js_api=Api()
    )
    _window.events.closing += _on_closing
    _start_tray(_build_tray())
    if sys.platform.startswith("linux"):
        webview.start(gui="qt", icon=APP_ICON)
    else:
        webview.start(icon=APP_ICON)      # blocks on the main thread
    _shutdown()


def main() -> None:
    if "--run-backend" in sys.argv:
        run_backend()
        return
    launch_desktop()


if __name__ == "__main__":
    main()
