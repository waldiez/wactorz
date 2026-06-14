"""Wactorz desktop shell.

A native window (PyWebView) wrapping the local Wactorz backend, with an
optional system-tray icon.

One process: this script is the window/shell. It launches the backend as a
child (`python -m wactorz`, or `<exe> --run-backend` when frozen) and points
the window at the backend's local web server.

Tray: uses Qt (QSystemTrayIcon), which the AppImage already bundles for the
webview. Builds are imported lazily, so a build without PySide6 (e.g. the
deb/rpm flavour, which uses the system WebKit2GTK webview) simply runs without
a tray rather than failing to start.
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
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

APP_NAME = "Wactorz"
APP_ID = "io.waldiez.wactorz"          # desktop-file id / WM_CLASS
HOST = "127.0.0.1"
PORT = int(os.environ.get("MONITOR_PORT", "8888"))
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

_backend: "subprocess.Popen | None" = None
_window = None
_hidden = False
_shown = False                 # the window is revealed only once the page paints
_tray = None                   # kept alive for the lifetime of the process
_tray_ok = False               # True only when a tray icon is actually shown

# Never-shown initial document. The window stays hidden until the real page has
# painted (see _on_app_loaded), so the only document the user sees is the app.
_BLANK_HTML = (
    '<!doctype html><html><body '
    'style="margin:0;height:100vh;background:#0A0E1A"></body></html>'
)
_ERROR_HTML = (
    '<!doctype html><html><body style="margin:0;height:100vh;display:flex;'
    'align-items:center;justify-content:center;font-family:sans-serif;'
    'background:#0A0E1A;color:#f87171;font-size:14px">'
    "Backend did not start — check logs and reopen.</body></html>"
)


# ── backend child ─────────────────────────────────────────────────────────────
def _spawn_backend() -> "subprocess.Popen":
    # Run from a per-user writable dir: the backend writes wactorz.log,
    # monitor.log and ./state relative to its cwd, which fails under an
    # all-users install (e.g. C:\Program Files). INTERFACE=rest makes it serve
    # the web/REST UI on MONITOR_PORT (its default "cli" mode never binds it).
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        MONITOR_PORT=str(PORT),
        INTERFACE="rest",
        WACTORZ_STATE_DIR=str(DATA_DIR / "state"),
    )
    cmd = [sys.executable, "--run-backend"] if FROZEN else [sys.executable, "-m", "wactorz"]
    try:
        log = open(BACKEND_LOG, "w")
        return subprocess.Popen(
            cmd, env=env, cwd=str(DATA_DIR), stdout=log, stderr=subprocess.STDOUT
        )
    except Exception:
        return subprocess.Popen(cmd, env=env, cwd=str(DATA_DIR))


def run_backend() -> None:
    """Entry for `<exe> --run-backend` (frozen) — same as `python -m wactorz`."""
    import runpy

    sys.argv = ["wactorz"]
    runpy.run_module("wactorz", run_name="__main__")


def _wait_for_backend(timeout: float = 30.0) -> bool:
    # Probe the REST /health endpoint (returns 200 once the server is up). "/"
    # depends on static assets and could 404, which would read as not-ready.
    health = f"{URL}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _backend is not None and _backend.poll() is not None:
            return False   # child exited before serving — see BACKEND_LOG
        try:
            with urllib.request.urlopen(health, timeout=1):
                return True
        except Exception:
            time.sleep(0.15)
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


# ── tray (Qt) ─────────────────────────────────────────────────────────────────
def _toggle() -> None:
    global _hidden
    if _window is None:
        return
    if _hidden:
        _window.show()
    else:
        _window.hide()
    _hidden = not _hidden


def _build_tray() -> bool:
    """Create a Qt system-tray icon. Returns True only if one is shown.

    PySide6 ships in the AppImage (same toolkit as the webview). Builds without
    it, or hosts without a tray area, get no tray and the function is a no-op.
    """
    global _tray, _tray_ok
    try:
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError:
        return False

    app = QApplication.instance() or QApplication(sys.argv)
    # Set the app identity so the window's WM_CLASS / Wayland app_id matches the
    # installed desktop file (taskbar grouping, correct icon).
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Wactorz")
    app.setOrganizationDomain("io.waldiez.wactorz")
    app.setDesktopFileName(APP_ID)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return False

    _tray = QSystemTrayIcon(QIcon(str(APP_ICON)))
    menu = QMenu()
    show_hide = QAction("Show / Hide", menu)
    show_hide.triggered.connect(_toggle)
    quit_item = QAction("Quit Wactorz", menu)
    quit_item.triggered.connect(_shutdown)
    menu.addAction(show_hide)
    menu.addAction(quit_item)
    _tray.setContextMenu(menu)
    _tray.activated.connect(
        lambda reason: _toggle()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    _tray.setToolTip(APP_NAME)
    _tray.show()
    _tray_ok = True
    return True


def _build_pystray_tray() -> bool:
    """macOS / Windows tray via pystray's native backend (no Qt, no GTK).

    Used only off Linux: pywebview runs a native loop there, so a Qt tray can't
    work, and pystray's darwin/win32 backends are lightweight (OS APIs, own
    thread). Returns True only if a tray is shown.
    """
    global _tray, _tray_ok
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return False
    try:
        image = Image.open(_ASSETS / "icon.png")   # PNG is always PIL-readable
    except Exception:
        return False

    menu = pystray.Menu(
        pystray.MenuItem("Show / Hide", lambda icon, item: _toggle(), default=True),
        pystray.MenuItem("Quit Wactorz", lambda icon, item: _shutdown()),
    )
    _tray = pystray.Icon("wactorz", image, APP_NAME, menu)
    try:
        _tray.run_detached()
    except Exception:
        return False   # e.g. macOS main-thread limitation — fall back to no tray
    _tray_ok = True
    return True


# ── lifecycle ─────────────────────────────────────────────────────────────────
def _set_app_user_model_id() -> None:
    """Windows: set the AppUserModelID so the taskbar groups the window under our
    icon. Must run before the window is created; a no-op on other platforms."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except Exception:
        pass


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
    """Close button: hide to tray if there is one, otherwise quit.

    Without a tray, hiding would leave the window unreachable, so we quit.
    """
    global _hidden
    if _tray_ok and _window is not None:
        _window.hide()
        _hidden = True
        return False   # cancel the real close
    _shutdown()
    return True


def _on_app_loaded(*_) -> None:
    """Reveal the window the first time the app document finishes loading."""
    global _shown
    if _shown or _window is None:
        return
    _shown = True
    _window.show()


def _load_when_ready(window) -> None:
    """Worker (runs after the GUI loop starts): wait for the backend, load the
    app, then reveal the window once its document has painted."""
    if _wait_for_backend():
        window.events.loaded += _on_app_loaded
        window.load_url(URL)
        time.sleep(2.0)        # fallback reveal if the 'loaded' event doesn't fire
        _on_app_loaded()
    else:
        _notify(APP_NAME, "Backend did not start in time")
        window.load_html(_ERROR_HTML)
        _on_app_loaded()


def launch_desktop() -> None:
    global _backend, _window
    # GNOME breaks the bundled QtWebEngine under Wayland (no webview content,
    # unthemed window), so force XWayland on GNOME only — KDE/others handle
    # Wayland fine. Overridable. AppRun does the same for the AppImage; this
    # covers source/dev runs that don't go through AppRun.
    if sys.platform.startswith("linux") and "gnome" in os.environ.get(
        "XDG_CURRENT_DESKTOP", ""
    ).lower():
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    _set_app_user_model_id()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _window = webview.create_window(
        APP_NAME,
        html=_BLANK_HTML,
        width=1200,
        height=700,
        min_size=(900, 600),
        js_api=Api(),
        background_color=SPLASH_BG,
    )
    _window.events.closing += _on_closing

    _backend = _spawn_backend()

    # Tray backend matches the webview backend: Qt on Linux (shares pywebview's
    # QApplication), pystray's native backend on macOS / Windows.
    if sys.platform.startswith("linux"):
        _build_tray()
    else:
        _build_pystray_tray()

    start_kwargs = {"icon": APP_ICON}
    if sys.platform.startswith("linux"):
        start_kwargs["gui"] = "qt"
    webview.start(_load_when_ready, _window, **start_kwargs)   # blocks the main thread
    _shutdown()


def main() -> None:
    if "--run-backend" in sys.argv:
        run_backend()
        return
    launch_desktop()


if __name__ == "__main__":
    main()
