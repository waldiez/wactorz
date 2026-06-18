"""System-tray icon.

Qt (QSystemTrayIcon) on Linux, where pywebview uses Qt; pystray's native
backend on macOS/Windows. Both builders are best-effort: they return the tray
object (which the caller must keep a reference to) or None when no tray can be
shown. All behaviour and state access is injected via TrayHooks, so this module
holds no shell state.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from wactorz.desktop.config import _ASSETS, APP_ICON, APP_ID, APP_NAME

Callback = Callable[[], None]


@dataclass
class TrayHooks:
    """Behaviour + state accessors the tray menu wires up."""
    on_toggle: Callback                       # show/hide the window
    on_check_updates: Callback                # manual update check
    on_quit: Callback                         # quit the app
    autostart_enabled: Callable[[], bool]
    set_autostart: Callable[[bool], None]
    auto_update_enabled: Callable[[], bool]
    set_auto_update: Callable[[bool], None]
    pending_update_version: Callable[[], str]  # "" when no update is known
    open_download: Callback                    # open the release page


def build_qt_tray(hooks: TrayHooks):
    """Create a Qt system-tray icon; return it, or None if PySide6 / a tray area
    is unavailable.

    PySide6 ships in the AppImage (same toolkit as the webview). Builds without
    it, or hosts without a tray area, get no tray.
    """
    try:
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError:
        return None

    app = QApplication.instance() or QApplication(sys.argv)
    # Set the app identity so the window's WM_CLASS / Wayland app_id matches the
    # installed desktop file (taskbar grouping, correct icon).
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Wactorz")
    app.setOrganizationDomain("io.waldiez.wactorz")
    app.setDesktopFileName(APP_ID)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None

    tray = QSystemTrayIcon(QIcon(str(APP_ICON)))
    menu = QMenu()
    show_hide = QAction("Show / Hide", menu)
    show_hide.triggered.connect(lambda *_: hooks.on_toggle())
    check_updates = QAction("Check for Updates...", menu)
    check_updates.triggered.connect(lambda *_: hooks.on_check_updates())
    download = QAction("Download update", menu)
    download.triggered.connect(lambda *_: hooks.open_download())

    def _refresh_download() -> None:
        version = hooks.pending_update_version()
        download.setVisible(bool(version))
        if version:
            download.setText(f"Download v{version}…")

    menu.aboutToShow.connect(_refresh_download)   # re-evaluate each time it opens
    _refresh_download()

    def _checkable(label: str, get: Callable[[], bool], set_: Callable[[bool], None]) -> QAction:
        action = QAction(label, menu)
        action.setCheckable(True)
        action.setChecked(get())

        def _on(checked: bool) -> None:
            set_(checked)
            action.setChecked(get())   # re-sync if the write failed

        action.triggered.connect(_on)
        return action

    autostart = _checkable("Start at login", hooks.autostart_enabled, hooks.set_autostart)
    auto_update = _checkable(
        "Check for updates automatically", hooks.auto_update_enabled, hooks.set_auto_update
    )
    quit_item = QAction("Quit Wactorz", menu)
    quit_item.triggered.connect(lambda *_: hooks.on_quit())

    menu.addAction(show_hide)
    menu.addAction(check_updates)
    menu.addAction(download)
    menu.addSeparator()
    menu.addAction(autostart)
    menu.addAction(auto_update)
    menu.addSeparator()
    menu.addAction(quit_item)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: hooks.on_toggle()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.setToolTip(APP_NAME)
    tray.show()
    return tray


def build_pystray_tray(hooks: TrayHooks):
    """macOS / Windows tray via pystray's native backend (no Qt, no GTK); return
    the icon, or None.

    Used only off Linux: pywebview runs a native loop there, so a Qt tray can't
    work, and pystray's darwin/win32 backends are lightweight (OS APIs, own
    thread).
    """
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(_ASSETS / "icon.png")   # PNG is always PIL-readable
    except Exception:
        return None

    def _toggler(get: Callable[[], bool], set_: Callable[[bool], None]):
        def _handler(icon, item) -> None:
            set_(not get())
            icon.update_menu()   # re-render the checkmark from the new state
        return _handler

    menu = pystray.Menu(
        pystray.MenuItem("Show / Hide", lambda icon, item: hooks.on_toggle(), default=True),
        pystray.MenuItem("Check for Updates...", lambda icon, item: hooks.on_check_updates()),
        pystray.MenuItem(lambda item: f"Download v{hooks.pending_update_version()}…",
                         lambda icon, item: hooks.open_download(),
                         visible=lambda item: bool(hooks.pending_update_version())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at login",
                         _toggler(hooks.autostart_enabled, hooks.set_autostart),
                         checked=lambda item: hooks.autostart_enabled()),
        pystray.MenuItem("Check for updates automatically",
                         _toggler(hooks.auto_update_enabled, hooks.set_auto_update),
                         checked=lambda item: hooks.auto_update_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Wactorz", lambda icon, item: hooks.on_quit()),
    )
    icon = pystray.Icon("wactorz", image, APP_NAME, menu)
    try:
        icon.run_detached()
    except Exception:
        return None   # e.g. macOS main-thread limitation — fall back to no tray
    return icon
