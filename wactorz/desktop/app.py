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

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import webview
from dotenv import find_dotenv, load_dotenv

from wactorz.desktop import (
    autostart,
    backend_config,
    desktop_entry,
    notifications,
    pages,
    settings,
    tray,
    updates,
    window_state,
)
from wactorz.desktop.config import (
    APP_ICON,
    APP_ID,
    APP_NAME,
    BACKEND_LOG,
    DATA_DIR,
    FROZEN,
    PORT,
    SPLASH_BG,
    URL,
)

load_dotenv(find_dotenv())

_backend: subprocess.Popen | None = None
_window = None
_hidden = False  # True when hidden to the tray (our hide(); no event)
_minimized = False  # tracked from the minimized/restored window events
_shown = False  # the window is revealed only once the page paints
_tray = None  # kept alive for the lifetime of the process
_tray_ok = False  # True only when a tray icon is actually shown


# ── backend child ─────────────────────────────────────────────────────────────
def _spawn_backend() -> subprocess.Popen:
    # Run from a per-user writable dir: the backend writes wactorz.log,
    # monitor.log and ./state relative to its cwd, which fails under an
    # all-users install (e.g. C:\Program Files). INTERFACE=rest makes it serve
    # the web/REST UI on MONITOR_PORT (its default "cli" mode never binds it).
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # User config from the Configure view wins over the inherited shell/.env, so
    # a stale shell var can't shadow it. The shell-owned vars below win over both.
    env.update(backend_config.env_for_backend())
    env.update(
        MONITOR_PORT=str(PORT),
        INTERFACE="rest",
        WACTORZ_STATE_DIR=str(DATA_DIR / "state"),
    )
    cmd = [sys.executable, "--run-backend"] if FROZEN else [sys.executable, "-m", "wactorz"]
    try:
        # Both the log handle and the child outlive this call: a `with` block
        # would close the pipe and terminate the backend on return.
        # pylint: disable=consider-using-with
        log = open(BACKEND_LOG, "w", encoding="utf-8")  # noqa: SIM115
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
            return False  # child exited before serving — see BACKEND_LOG
        try:
            with urllib.request.urlopen(health, timeout=1):
                return True
        except Exception:
            time.sleep(0.15)
    return False


def _mqtt_reachable(timeout: float = 3.0) -> bool:
    """TCP-probe the configured MQTT broker. The backend can't start without it,
    so a quick check lets us open Configure instead of waiting out the timeout.
    """
    env = backend_config.env_for_backend()
    host = env.get("MQTT_HOST") or os.environ.get("MQTT_HOST") or "localhost"
    try:
        port = int(env.get("MQTT_PORT") or os.environ.get("MQTT_PORT") or 1883)
    except (TypeError, ValueError):
        port = 1883
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_mqtt(timeout: float = 25.0) -> bool:
    """Poll the MQTT broker until reachable or timeout. Covers the broker (often
    a local Docker container) still starting up at login — a single probe would
    lose that race and pop Configure even though it comes up a moment later.
    """
    deadline = time.time() + timeout
    while True:
        if _mqtt_reachable(timeout=2.0):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def _show_config(message: str = "") -> None:
    """Load the Configure form. Offer Cancel only when the backend is running,
    i.e. there's a live app to return to (not on first-run / failure).
    """
    if _window is not None:
        backend_up = _backend is not None and _backend.poll() is None
        _window.load_html(pages.config_html(backend_config.load(), message, can_cancel=backend_up))
        _reveal_window()  # bring it forward if opened from the tray while hidden


def _defer_nav(action) -> None:
    """Run a window navigation just after the current JS-API call returns.
    Navigating synchronously inside an Api method makes pywebview deliver the
    call's return value on the new page (where the callback is gone) and raise,
    so hand it to a worker that lets the call return first.
    """

    def _run():
        time.sleep(0.05)
        try:
            action()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _stop_backend() -> None:
    global _backend
    if _backend and _backend.poll() is None:
        _backend.terminate()
        try:
            _backend.wait(timeout=5)
        except Exception:
            _backend.kill()
    _backend = None


def _restart_backend() -> None:
    """Apply saved config: stop the child, show the splash, respawn with the new
    env, then load the app (or back to Configure / the error page). Off-thread.
    """
    global _backend
    _stop_backend()
    if not _mqtt_reachable():
        _show_config("MQTT broker still unreachable — check the host and port.")
        return
    if _window is not None:
        _window.load_html(pages.LOADING_HTML)
    _backend = _spawn_backend()
    if _wait_for_backend():
        if _window is not None:
            _window.load_url(URL)
    elif _window is not None:
        _window.load_html(pages.error_html(str(BACKEND_LOG)))


# ── JS bridge (window.pywebview.api.*) ──────────────────────────────────────
class Api:
    """Methods the shell's own pages call as ``window.pywebview.api.*``.

    pywebview injects this bridge into every page it loads, but only the
    splash/error/config pages (``pages.py``) use it — the dashboard SPA never
    does. Keep these thin: they run on pywebview's JS-API thread, so anything
    that navigates the window must go through ``_defer_nav``.
    """

    def notify(self, title: str, body: str) -> None:
        """Raise an OS notification on behalf of the page."""
        notifications.notify(title, body)

    def open_config(self) -> None:
        """Load the Configure form into the window (called from the error page)."""
        _defer_nav(_show_config)

    def close_config(self) -> None:
        """Cancel: return to the running app without saving."""

        def _load() -> None:
            if _window is not None:
                _window.load_url(URL)

        _defer_nav(_load)

    def retry(self) -> bool:
        """Retry connecting with the current config (e.g. after starting the
        broker) without saving — restart the backend off-thread.
        """
        threading.Thread(target=_restart_backend, daemon=True).start()
        return True

    def save_config(self, values: dict) -> bool:
        """Persist config, then restart the backend off-thread so the call
        returns promptly; the window reloads when the restart finishes.
        """
        backend_config.save(values or {})
        threading.Thread(target=_restart_backend, daemon=True).start()
        return True


# ── tray ────────────────────────────────────────────────────────────────────
def _on_minimized(*_) -> None:
    global _minimized
    _minimized = True


def _on_restored(*_) -> None:
    global _minimized
    _minimized = False


def _toggle() -> None:
    """Tray Show/Hide. Bases the decision on real window state — a native
    minimize (no hide event) would otherwise desync a simple flag and make the
    first click a no-op.
    """
    global _hidden
    if _window is None:
        return
    if _hidden or _minimized:
        _reveal_window()
    else:
        _window.hide()
        _hidden = True


# ── lifecycle ─────────────────────────────────────────────────────────────────
def _set_app_user_model_id() -> None:
    """Windows: set the AppUserModelID so the taskbar groups the window under our
    icon. Must run before the window is created; a no-op on other platforms.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except Exception:
        pass


def _shutdown(*_) -> None:
    window_state.save()
    _stop_backend()
    os._exit(0)


def _on_closing() -> bool:
    """Close button: hide to tray if there is one, otherwise quit.

    Without a tray, hiding would leave the window unreachable, so we quit.
    """
    global _hidden
    if _tray_ok and _window is not None:
        _window.hide()
        _hidden = True
        return False  # cancel the real close
    _shutdown()
    return True


def _on_app_loaded(*_) -> None:
    """Reveal the window the first time the app document finishes loading."""
    global _shown
    if _shown or _window is None:
        return
    _shown = True
    _window.show()


# Retained: NSApp's delegate is a non-owning reference.
_macos_app_delegate = None


def _reveal_window() -> None:
    """Bring the window back whether it was hidden to the tray or minimized."""
    global _hidden, _minimized
    if _window is None:
        return
    if _minimized:
        _window.restore()
        _minimized = False
    _window.show()
    _hidden = False


def _install_macos_quit_handler() -> None:
    """macOS: Dock-Quit / ⌘Q go through NSApp's applicationShouldTerminate, which
    pywebview gates on the window 'closing' veto — so hide-to-tray also blocks
    quitting. Install an app delegate that quits for real. The red close button
    (windowShouldClose) is untouched, so it still hides to the tray.
    """
    if sys.platform != "darwin":
        return
    try:
        # pylint: disable=import-error
        from AppKit import NSApplication, NSTerminateNow  # pyright: ignore[reportMissingImports]
        from Foundation import NSObject  # pyright: ignore[reportMissingImports]
        from PyObjCTools import AppHelper  # pyright: ignore[reportMissingImports]

        global _macos_app_delegate

        class _QuitDelegate(NSObject):
            """Handles the AppKit events pywebview does not surface itself."""

            def applicationShouldTerminate_(self, app):
                """Shut down cleanly on Dock ▸ Quit and ⌘Q.

                Both bypass the window close handler, so the state save and
                backend stop have to happen here or the child outlives us.
                """
                _shutdown()  # saves state, stops backend, os._exit
                return NSTerminateNow  # belt-and-suspenders if _shutdown returns

            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
                """Restore the window when the Dock icon is clicked.

                With nothing visible (hidden to the tray) macOS would
                otherwise do nothing, leaving the app looking dead.
                """
                # Dock-icon click while hidden to the tray → bring the window back.
                if not has_visible:
                    _reveal_window()
                return True

            def applicationSupportsSecureRestorableState_(self, app):
                """Opt in to secure state restoration (quiets a macOS warning).

                The shell restores its own geometry from disk, so there is no
                AppKit-managed state to protect.
                """
                return True

        _macos_app_delegate = _QuitDelegate.alloc().init()
        # Set on the main thread (we run on a pywebview worker), after pywebview
        # has already installed its own delegate during window creation.
        AppHelper.callAfter(
            lambda: NSApplication.sharedApplication().setDelegate_(_macos_app_delegate)
        )
    except Exception:
        pass


def _load_when_ready(window) -> None:
    """Worker (runs after the GUI loop starts): correct the window placement,
    wait for MQTT, start the backend, load the app, then reveal the window.
    """
    global _backend
    window_state.place(_window, webview.screens)
    _install_macos_quit_handler()
    notifications.request_authorization()  # macOS: prompt for permission once
    if not _wait_for_mqtt():
        # The backend can't start without MQTT; configure instead of failing.
        notifications.notify(APP_NAME, "MQTT broker unreachable — opening configuration.")
        _show_config("MQTT broker unreachable — check the host and port.")
        _on_app_loaded()
    else:
        # Spawn only once MQTT is up, so the backend doesn't exit on a boot race.
        _backend = _spawn_backend()
        if _wait_for_backend():
            window.events.loaded += _on_app_loaded
            window.load_url(URL)
            time.sleep(2.0)  # fallback reveal if the 'loaded' event doesn't fire
            _on_app_loaded()
        else:
            notifications.notify(APP_NAME, "Backend did not start in time")
            window.load_html(pages.error_html(str(BACKEND_LOG)))
            _on_app_loaded()

    # Background update check on launch when enabled — silent unless one is found.
    if settings.auto_update_check():
        updates.check_for_updates(interactive=False)


def _qt_available() -> bool:
    """True if PySide6 is importable — the Qt webview + tray backend."""
    return importlib.util.find_spec("PySide6") is not None


def _gtk_available() -> bool:
    """True if PyGObject (the GTK/WebKit2 webview backend) is importable."""
    return importlib.util.find_spec("gi") is not None


def launch_desktop() -> None:
    """Open the app window and run the GUI loop until the user quits.

    Picks a GUI backend (Qt where available, else the system GTK/WebKit),
    restores the last window geometry, and builds a tray if one can be shown.
    The backend child is started once the loop is up. Blocks the main thread.
    """
    global _window, _tray, _tray_ok
    # On Linux we prefer the Qt (QtWebEngine) backend the AppImage bundles, but a
    # source/pip install without PySide6 (e.g. a system WebKit2GTK box such as a
    # Raspberry Pi) should fall back to pywebview's GTK backend rather than crash.
    # Only force gui="qt" — and the Qt-only tweaks below — when Qt is present.
    _linux = sys.platform.startswith("linux")
    if _linux:
        # Install the menu entry + icon so the desktop portal can resolve the app
        # id (silences the QDBus "App info not found" warning) and the taskbar
        # shows our icon. Before window creation, when Qt registers with the portal.
        desktop_entry.install()
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

    state = window_state.load()
    window_state.seed(state)  # so an untouched session re-saves the same
    _window = webview.create_window(
        APP_NAME,
        html=pages.LOADING_HTML,
        width=state["width"],
        height=state["height"],
        x=state["x"],
        y=state["y"],
        min_size=(900, 600),
        js_api=Api(),
        background_color=SPLASH_BG,
    )
    if not _window:
        return
    _window.events.closing += _on_closing
    _window.events.resized += window_state.track_resize
    _window.events.moved += window_state.track_move
    _window.events.minimized += _on_minimized
    _window.events.restored += _on_restored

    def _set_autostart(enabled: bool) -> None:
        autostart.set_enabled(enabled)

    # The backend is spawned later, in _load_when_ready, once MQTT is reachable
    # (see _wait_for_mqtt) — spawning here would race a still-booting broker.

    # Tray backend matches the webview backend: the Qt tray (QSystemTrayIcon,
    # shares pywebview's QApplication) when Qt is used, otherwise pystray's
    # native backend — macOS/Windows, or the Linux GTK fallback when pystray is
    # installed (wactorz[desktop-gtk]). If no tray can be shown, _tray_ok stays
    # False and closing the window shuts the app down instead of hiding it.
    hooks = tray.TrayHooks(
        on_toggle=_toggle,
        on_configure=_show_config,
        on_check_updates=updates.check_for_updates,
        on_quit=_shutdown,
        autostart_enabled=autostart.is_enabled,
        set_autostart=_set_autostart,
        auto_update_enabled=settings.auto_update_check,
        set_auto_update=settings.set_auto_update_check,
        pending_update_version=updates.pending_version,
        open_download=updates.open_download,
    )
    _tray = (tray.build_qt_tray if _use_qt else tray.build_pystray_tray)(hooks)
    _tray_ok = _tray is not None
    if _use_qt:
        webview.start(_load_when_ready, [_window], icon=str(APP_ICON), gui="qt")
    else:
        webview.start(_load_when_ready, [_window], icon=str(APP_ICON))
    _shutdown()


def main() -> None:
    """Console-script entry point (``wactorz-desktop``).

    Re-invoked with ``--run-backend`` for the backend child process; without
    it, opens the desktop window.
    """
    if "--run-backend" in sys.argv:
        run_backend()
        return
    launch_desktop()


if __name__ == "__main__":
    main()
