"""Wactorz desktop shell: native window (PyWebView) + tray (pystray) wrapping
the Python backend.
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
ICON_EXT = {"win32": "ico", "darwin": "icns"}.get(sys.platform, "png")
APP_ICON = Path(__file__).with_name("assets") / f"icon.{ICON_EXT}"
SPLASH_BG = "#0A0E1A"          # window surface colour while the app document paints

_backend: "subprocess.Popen | None" = None
_window = None
_hidden = False
_shown = False                 # window is revealed only once the app has painted


# ── Initial / error documents ─────────────────────────────────────────────────
# The window stays hidden until the app page has painted — its in-page boot
# overlay (frontend/index.html) is the single splash, so the only document the
# user ever sees is the app itself (no native splash → app doc-swap flash, no
# WebKitGTK white). This blank dark page is the never-shown initial document.
_BLANK_HTML = (
    '<!doctype html><html><body '
    'style="margin:0;height:100vh;background:#0A0E1A"></body></html>'
)
_ERROR_HTML = (
    '<!doctype html><html><body style="margin:0;height:100vh;display:flex;'
    'align-items:center;justify-content:center;font-family:sans-serif;'
    'background:#0A0E1A;color:#f87171;font-size:14px">'
    'Backend did not start — check logs and reopen.</body></html>'
)


# ── backend child ───────────────────────────────────────────────────────────
# The backend log — child stdout/stderr go here so failures are diagnosable
# (a frozen app has no console). Referenced in the error page.
BACKEND_LOG = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wactorz" / "backend.log"


def _spawn_backend() -> "subprocess.Popen":
    # INTERFACE=rest is essential: without it the backend defaults to "cli"
    # (interactive terminal mode) outside DEV_MODE and never serves the web UI,
    # so the window can never load. We want the REST/web server on MONITOR_PORT.
    env = dict(os.environ, MONITOR_PORT=str(PORT), INTERFACE="rest")
    cmd = [sys.executable, "--run-backend"] if FROZEN else [sys.executable, "-m", "wactorz"]
    try:
        BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(BACKEND_LOG, "w")
        return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    except Exception:
        # Fall back to inherited stdio if the log file can't be opened.
        return subprocess.Popen(cmd, env=env)


def run_backend() -> None:
    """Invoked as `<exe> --run-backend` (frozen) — equivalent to `python -m wactorz`."""
    import runpy
    sys.argv = ["wactorz"]
    runpy.run_module("wactorz", run_name="__main__")


def _wait_for_backend(timeout: float = 30.0) -> bool:
    # Probe the REST /health endpoint (always 200 once the server is up) rather
    # than "/", which depends on static assets and would 404 → false negative.
    health = f"{URL}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _backend is not None and _backend.poll() is not None:
            return False   # child already exited — see BACKEND_LOG
        try:
            with urllib.request.urlopen(health, timeout=1):
                return True
        except Exception:
            time.sleep(0.15)   # poll often so we swap in the app the moment it's up
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


def _on_app_loaded(*_) -> None:
    """Reveal the window the first time the app document finishes loading."""
    global _shown
    if _shown or _window is None:
        return
    _shown = True
    _window.show()


def launch_desktop() -> None:
    global _backend, _window
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _window = webview.create_window(
        APP_NAME, html=_BLANK_HTML,
        width=1200, height=700, min_size=(900, 600), js_api=Api(),
        background_color=SPLASH_BG,
    )
    _backend = _spawn_backend()

    _window.events.closing += _on_closing
    _start_tray(_build_tray())

    # Webview backend selection (Linux ships two flavours):
    #   • AppImage   — only PySide6 is bundled → WACTORZ_WEBVIEW_GUI=qt
    #   • deb / rpm  — system WebKit2GTK present → unset, pywebview picks gtk
    # Unset/empty means let pywebview auto-detect. macOS/Windows ignore this.
    _gui = os.environ.get("WACTORZ_WEBVIEW_GUI", "").strip().lower()
    _start_kwargs = {"icon": APP_ICON}
    if _gui:
        _start_kwargs["gui"] = _gui
    # _load_when_ready runs on a worker thread once the GUI loop is up.
    webview.start(_load_when_ready, _window, **_start_kwargs)   # blocks main thread
    _shutdown()


def _load_when_ready(window) -> None:
    """Worker thread: wait for the backend, load the app, then reveal the window.

    The window is shown by _on_app_loaded once the document paints (its in-page
    boot overlay covers the SPA boot), so there's no visible splash→app
    transition and no white flash.
    """
    if _wait_for_backend():
        window.events.loaded += _on_app_loaded
        window.load_url(URL)
        time.sleep(2.0)        # fallback reveal in case 'loaded' doesn't fire
        _on_app_loaded()
    else:
        _notify(APP_NAME, "Backend did not start in time")
        window.load_html(_ERROR_HTML)
        _on_app_loaded()


def main() -> None:
    if "--run-backend" in sys.argv:
        run_backend()
        return
    launch_desktop()


if __name__ == "__main__":
    main()
