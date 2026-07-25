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
import sys
import threading
import time

import webview
from dotenv import find_dotenv, load_dotenv

from wactorz.desktop import (
    autostart,
    backend,
    backend_config,
    desktop_entry,
    notifications,
    pages,
    platform_hooks,
    settings,
    tray,
    updates,
    window_state,
)
from wactorz.desktop.config import (
    APP_ICON,
    APP_NAME,
    BACKEND_LOG,
    SPLASH_BG,
    URL,
)

load_dotenv(find_dotenv())

_window = None
_hidden = False  # True when hidden to the tray (our hide(); no event)
_minimized = False  # tracked from the minimized/restored window events
_shown = False  # the window is revealed only once the page paints
_tray = None  # kept alive for the lifetime of the process
_tray_ok = False  # True only when a tray icon is actually shown


def _show_config(message: str = "") -> None:
    """Load the Configure form. Offer Cancel only when the backend is running,
    i.e. there's a live app to return to (not on first-run / failure).
    """
    if _window is not None:
        backend_up = backend.is_running()
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


def _restart_backend() -> None:
    """Apply saved config: stop the child, show the splash, respawn with the new
    env, then load the app (or back to Configure / the error page). Off-thread.
    """
    backend.stop()
    if not backend.mqtt_reachable():
        _show_config("MQTT broker still unreachable — check the host and port.")
        return
    if _window is not None:
        _window.load_html(pages.LOADING_HTML)
    backend.start()
    if backend.wait_until_serving():
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
def _shutdown(*_) -> None:
    window_state.save()
    backend.stop()
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


def _load_when_ready(window) -> None:
    """Worker (runs after the GUI loop starts): correct the window placement,
    wait for MQTT, start the backend, load the app, then reveal the window.
    """
    window_state.place(_window, webview.screens)
    platform_hooks.install_quit_handler(on_quit=_shutdown, on_reopen=_reveal_window)
    notifications.request_authorization()  # macOS: prompt for permission once
    if not backend.wait_for_mqtt():
        # The backend can't start without MQTT; configure instead of failing.
        notifications.notify(APP_NAME, "MQTT broker unreachable — opening configuration.")
        _show_config("MQTT broker unreachable — check the host and port.")
        _on_app_loaded()
    else:
        # Spawn only once MQTT is up, so the backend doesn't exit on a boot race.
        backend.start()
        if backend.wait_until_serving():
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
    platform_hooks.set_app_user_model_id()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    state = window_state.load()
    positioning_supported = window_state.position_supported()
    window_state.seed(state)  # so an untouched session re-saves the same
    _window = webview.create_window(
        APP_NAME,
        html=pages.LOADING_HTML,
        width=state["width"],
        height=state["height"],
        # Wayland ignores a client-chosen position (the compositor places the
        # window), so don't ask for one there — see window_state.
        x=state["x"] if positioning_supported else None,
        y=state["y"] if positioning_supported else None,
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
    # (see backend.wait_for_mqtt) — spawning here would race a still-booting broker.

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
        backend.run_in_process()
        return
    launch_desktop()


if __name__ == "__main__":
    main()
