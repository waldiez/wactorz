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

import json
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
# Last window geometry, restored on the next launch.
WINDOW_STATE_FILE = DATA_DIR / "window_state.json"

_DEFAULT_WINDOW_STATE = {"width": 1280, "height": 720, "x": None, "y": None}

_backend: "subprocess.Popen | None" = None
_window = None
_hidden = False
_shown = False                 # the window is revealed only once the page paints
_tray = None                   # kept alive for the lifetime of the process
_tray_ok = False               # True only when a tray icon is actually shown
# Live geometry, kept fresh by the resized/moved events so we never have to read
# a hidden or torn-down window at shutdown. Seeded from the saved state on launch.
_window_geometry = dict(_DEFAULT_WINDOW_STATE)


def _sanitize_window_state(s: dict) -> dict:
    """Coerce a loaded state dict to valid values (it may be hand-edited or a
    partial/old file): positive int width/height; x/y int or None."""
    out = dict(_DEFAULT_WINDOW_STATE)
    try:
        w, h = int(s["width"]), int(s["height"])
        if w >= 1 and h >= 1:
            out["width"], out["height"] = w, h
    except (KeyError, TypeError, ValueError):
        pass
    for k in ("x", "y"):
        try:
            out[k] = None if s.get(k) is None else int(s[k])
        except (TypeError, ValueError):
            out[k] = None
    return out


def _load_window_state() -> dict:
    """Last saved window geometry, sanitized — or defaults. Never raises; a
    missing or corrupt file just yields the defaults."""
    try:
        if WINDOW_STATE_FILE.exists():
            return _sanitize_window_state(json.loads(WINDOW_STATE_FILE.read_text()))
    except Exception:
        pass
    return dict(_DEFAULT_WINDOW_STATE)


def _on_window_resized(width, height) -> None:
    _window_geometry["width"], _window_geometry["height"] = int(width), int(height)


def _on_window_moved(x, y) -> None:
    _window_geometry["x"], _window_geometry["y"] = int(x), int(y)


def _place_window() -> None:
    """After the GUI is up (screens are known): keep the window on a connected
    screen and no larger than it. Fixes a saved position on a monitor that is no
    longer present, and a saved size larger than the current display."""
    if _window is None:
        return
    try:
        screens = webview.screens or []
        if not screens:
            return
        w, h = _window_geometry["width"], _window_geometry["height"]
        x, y = _window_geometry["x"], _window_geometry["y"]

        def _on(scr, px, py):
            return scr.x <= px < scr.x + scr.width and scr.y <= py < scr.y + scr.height

        # No saved position (first run / never moved): the window is already
        # centered by create_window — only shrink it if it overflows the primary.
        if x is None or y is None:
            primary = screens[0]
            nw, nh = min(w, primary.width), min(h, primary.height)
            if (nw, nh) != (w, h):
                _window.resize(nw, nh)
                _window_geometry.update(width=nw, height=nh)
            return

        target = next((s for s in screens if _on(s, x, y)), None)
        nw = min(w, (target or screens[0]).width)
        nh = min(h, (target or screens[0]).height)
        if target is not None:
            # On a live screen: keep the position, nudge so it fits within it.
            nx = min(max(x, target.x), target.x + target.width - nw)
            ny = min(max(y, target.y), target.y + target.height - nh)
        else:
            # Saved screen is gone: center on the primary screen.
            primary = screens[0]
            nx = primary.x + (primary.width - nw) // 2
            ny = primary.y + (primary.height - nh) // 2
        if (nw, nh) != (w, h):
            _window.resize(nw, nh)
        if (nx, ny) != (x, y):
            _window.move(nx, ny)
        _window_geometry.update(width=nw, height=nh, x=nx, y=ny)
    except Exception:
        pass


def _save_window_state() -> None:
    """Persist the tracked geometry. Best-effort — never raises. Reads the
    tracked dict (not the live window), so it is correct even when quitting from
    the tray with the window hidden."""
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_FILE.write_text(json.dumps(_window_geometry))
    except Exception:
        pass

# Loading splash shown while the backend child starts (a few seconds, longer if
# it is waiting on an MQTT broker). Self-contained — no external assets, since it
# must paint before the backend is up. Replaced by the app URL once the backend
# answers, or by _ERROR_HTML if it never does.
_LOADING_HTML = """\
<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%}
  body{background:#0A0E1A;color:#e2e8f0;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1.25rem}
  .ring{width:44px;height:44px;border:3px solid #1e2640;border-top-color:#6366f1;
        border-radius:50%;animation:spin .9s linear infinite}
  .name{font-size:1.15rem;font-weight:600;letter-spacing:.04em;color:#c7d2fe}
  .sub{font-size:.8rem;color:#64748b}
  @keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
  <div class="ring"></div>
  <div class="name">Wactorz</div>
  <div class="sub">Starting...</div>
</body></html>"""

# Shown if the backend never answers. The usual cause is an unreachable MQTT
# broker, so we say so and point at the log.
_ERROR_HTML = f"""\
<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;height:100%}}
  body{{background:#0A0E1A;color:#e2e8f0;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;align-items:center;justify-content:center}}
  .card{{max-width:480px;padding:2rem;text-align:center}}
  h1{{font-size:1.1rem;color:#f87171;margin:0 0 .75rem}}
  p{{font-size:.85rem;line-height:1.55;color:#94a3b8;margin:.4rem 0}}
  code{{background:#11182e;padding:.1rem .35rem;border-radius:4px;color:#cbd5e1;
        font-size:.78rem;word-break:break-all}}
</style></head><body><div class="card">
  <h1>Wactorz backend didn’t start</h1>
  <p>The most common cause is that the configured <b>MQTT broker is unreachable</b>.
     Make sure your MQTT host is running and reachable from this machine, then reopen.</p>
  <p>Details are in the log:<br><code>{BACKEND_LOG}</code></p>
</div></body></html>"""


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


# Retained because NSUserNotificationCenter.delegate is a non-owning reference —
# if the Python delegate is collected, foreground presentation stops working.
_macos_notif_delegate = None


def _notify_macos_native(title: str, body: str) -> None:
    """Post an NSUserNotification via pyobjc (already provided by pywebview's
    Cocoa backend — no extra dependency). Installs a delegate that forces the
    banner to show even when Wactorz is the frontmost app; macOS suppresses it
    for the active app otherwise."""
    global _macos_notif_delegate
    try:
        from Foundation import NSObject, NSUserNotification, NSUserNotificationCenter

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            return
        if _macos_notif_delegate is None:
            class _PresentAlways(NSObject):
                def userNotificationCenter_shouldPresentNotification_(self, center, note):
                    return True

            _macos_notif_delegate = _PresentAlways.alloc().init()
        center.setDelegate_(_macos_notif_delegate)
        note = NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(body)
        center.deliverNotification_(note)
    except Exception:
        pass


def _notify(title: str, body: str) -> None:
    # macOS: post NSUserNotification ourselves via pyobjc with a delegate that
    # presents the banner even when we are frontmost. plyer's macOS backend goes
    # through pyobjus, whose delegate support is unreliable, so it only shows when
    # backgrounded — and pyobjc needs no pyobjus dependency.
    if sys.platform == "darwin":
        _notify_macos_native(title, body)
        return
    try:
        from plyer import notification

        notification.notify(title=title, message=body, app_name=APP_NAME)
    except Exception:
        pass


# ── updates ─────────────────────────────────────────────────────────────────
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


def _check_for_updates() -> None:
    """Manual update check (tray). Runs off the GUI thread so it never blocks."""
    import threading

    threading.Thread(target=_update_check_task, daemon=True).start()


def _update_check_task() -> None:
    try:
        from wactorz import __version__ as current

        req = urllib.request.Request(
            _LATEST_RELEASE_URL,
            headers={"User-Agent": "Wactorz-Desktop", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latest = json.loads(resp.read().decode()).get("tag_name", "").lstrip("v")
        if latest and _is_newer(latest, current):
            _notify(APP_NAME, f"Update available: v{latest} (you have v{current}).")
        else:
            _notify(APP_NAME, f"Wactorz is up to date (v{current}).")
    except Exception:
        _notify(APP_NAME, "Could not check for updates — see your connection and try again.")


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
    check_updates = QAction("Check for Updates...", menu)
    check_updates.triggered.connect(_check_for_updates)
    quit_item = QAction("Quit Wactorz", menu)
    quit_item.triggered.connect(_shutdown)
    menu.addAction(show_hide)
    menu.addAction(check_updates)
    menu.addSeparator()
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
        pystray.MenuItem("Check for Updates...", lambda icon, item: _check_for_updates()),
        pystray.Menu.SEPARATOR,
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
    _save_window_state()
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
    """Worker (runs after the GUI loop starts): correct the window placement now
    that screens are known, wait for the backend, load the app, then reveal the
    window once its document has painted."""
    _place_window()
    if _wait_for_backend():
        window.events.loaded += _on_app_loaded
        window.load_url(URL)
        time.sleep(2.0)        # fallback reveal if the 'loaded' event doesn't fire
        _on_app_loaded()
    else:
        _notify(APP_NAME, "Backend did not start in time")
        window.load_html(_ERROR_HTML)
        _on_app_loaded()


def _qt_available() -> bool:
    """True if PySide6 is importable — the Qt webview + tray backend."""
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


def _gtk_available() -> bool:
    """True if PyGObject (the GTK/WebKit2 webview backend) is importable."""
    import importlib.util
    return importlib.util.find_spec("gi") is not None


def launch_desktop() -> None:
    global _backend, _window
    # On Linux we prefer the Qt (QtWebEngine) backend the AppImage bundles, but a
    # source/pip install without PySide6 (e.g. a system WebKit2GTK box such as a
    # Raspberry Pi) should fall back to pywebview's GTK backend rather than crash.
    # Only force gui="qt" — and the Qt-only tweaks below — when Qt is present.
    _linux = sys.platform.startswith("linux")
    _use_qt = _linux and _qt_available()
    # No install-time guarantee of a Linux backend (PySide6/GTK are separate
    # extras), so fail clearly here instead of crashing deep inside pywebview.
    if _linux and not _use_qt and not _gtk_available():
        sys.stderr.write(
            "wactorz-desktop: no GUI backend found on Linux. Install one of:\n"
            "  pip install 'wactorz[desktop-qt]'                  # Qt (PySide6)\n"
            "  sudo apt install python3-gi gir1.2-webkit2-4.1     # system GTK/WebKit\n"
        )
        sys.exit(1)
    # GNOME breaks the bundled QtWebEngine under Wayland (no webview content,
    # unthemed window), so force XWayland on GNOME only — KDE/others handle
    # Wayland fine. Overridable. AppRun does the same for the AppImage; this
    # covers source/dev runs that don't go through AppRun. (Qt-only.)
    if _use_qt and "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    _set_app_user_model_id()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    state = _load_window_state()
    _window_geometry.update(state)   # so an untouched session re-saves the same
    _window = webview.create_window(
        APP_NAME,
        html=_LOADING_HTML,
        width=state["width"],
        height=state["height"],
        x=state["x"],
        y=state["y"],
        min_size=(900, 600),
        js_api=Api(),
        background_color=SPLASH_BG,
    )
    _window.events.closing += _on_closing
    _window.events.resized += _on_window_resized
    _window.events.moved += _on_window_moved

    _backend = _spawn_backend()

    # Tray backend matches the webview backend: the Qt tray (QSystemTrayIcon,
    # shares pywebview's QApplication) when Qt is used, otherwise pystray's
    # native backend — macOS/Windows, or the Linux GTK fallback when pystray is
    # installed (wactorz[desktop-gtk]). If no tray can be shown, _tray_ok stays
    # False and closing the window shuts the app down instead of hiding it.
    if _use_qt:
        _build_tray()
    else:
        _build_pystray_tray()

    start_kwargs = {"icon": APP_ICON}
    if _use_qt:
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
