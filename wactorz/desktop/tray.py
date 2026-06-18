"""System-tray icon.

Qt (QSystemTrayIcon) on Linux, where pywebview uses Qt; pystray's native
backend on macOS/Windows. Both builders are best-effort: they return the tray
object (which the caller must keep a reference to) or None when no tray can be
shown. The window/quit/update behaviours are passed in as callbacks so this
module holds no shell state.
"""
from __future__ import annotations

import sys
from typing import Callable

from wactorz.desktop.config import _ASSETS, APP_ICON, APP_ID, APP_NAME

Callback = Callable[[], None]


def build_qt_tray(
    on_toggle: Callback,
    on_check_updates: Callback,
    on_quit: Callback,
    autostart_enabled: Callable[[], bool],
    set_autostart: Callable[[bool], None],
):
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
    show_hide.triggered.connect(on_toggle)
    check_updates = QAction("Check for Updates...", menu)
    check_updates.triggered.connect(on_check_updates)
    autostart = QAction("Start at login", menu)
    autostart.setCheckable(True)
    autostart.setChecked(autostart_enabled())

    def _toggle_autostart(checked: bool) -> None:
        set_autostart(checked)
        autostart.setChecked(autostart_enabled())   # re-sync if the write failed

    autostart.triggered.connect(_toggle_autostart)
    quit_item = QAction("Quit Wactorz", menu)
    quit_item.triggered.connect(on_quit)
    menu.addAction(show_hide)
    menu.addAction(check_updates)
    menu.addSeparator()
    menu.addAction(autostart)
    menu.addSeparator()
    menu.addAction(quit_item)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: on_toggle()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.setToolTip(APP_NAME)
    tray.show()
    return tray


def build_pystray_tray(
    on_toggle: Callback,
    on_check_updates: Callback,
    on_quit: Callback,
    autostart_enabled: Callable[[], bool],
    set_autostart: Callable[[bool], None],
):
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

    def _toggle_autostart(icon, item) -> None:
        set_autostart(not autostart_enabled())
        icon.update_menu()   # re-render the checkmark from the new state

    menu = pystray.Menu(
        pystray.MenuItem("Show / Hide", lambda icon, item: on_toggle(), default=True),
        pystray.MenuItem("Check for Updates...", lambda icon, item: on_check_updates()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at login", _toggle_autostart,
                         checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Wactorz", lambda icon, item: on_quit()),
    )
    icon = pystray.Icon("wactorz", image, APP_NAME, menu)
    try:
        icon.run_detached()
    except Exception:
        return None   # e.g. macOS main-thread limitation — fall back to no tray
    return icon
