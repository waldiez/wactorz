"""OS integration the webview toolkit does not handle for us.

Two pieces of per-platform ceremony that the shell needs but that have nothing
to do with each other beyond being conditional on the OS: the macOS app delegate
(so Dock ▸ Quit really quits) and the Windows AppUserModelID (so the taskbar
groups the window under our icon). Linux's counterpart — the .desktop entry —
is large enough to live in :mod:`wactorz.desktop.desktop_entry`.

Both are best-effort no-ops off their platform, and both swallow failures: a
missing Objective-C bridge or a stubborn shell API is a cosmetic loss, never a
reason to fail a launch.

Callbacks are injected rather than imported so this module never reaches back
into the shell.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable

from wactorz.desktop.config import APP_ID

logger = logging.getLogger(__name__)

# The delegate must outlive this call: AppKit holds it weakly, so a local would
# be collected and the app would fall back to pywebview's own delegate.
_macos_app_delegate = None


def set_app_user_model_id() -> None:
    """Group the taskbar entry under our own icon (Windows only).

    Without this, Windows groups the window under the host interpreter — the
    app shows up as "Python". Must run before the window is created.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # type: ignore[attr-defined]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("[desktop] could not set the AppUserModelID: %s", exc)


def install_quit_handler(on_quit: Callable[[], None], on_reopen: Callable[[], None]) -> None:
    """Make Dock ▸ Quit and ⌘Q shut down for real (macOS only).

    Both go through NSApp's ``applicationShouldTerminate``, which pywebview gates
    on the window's 'closing' veto — so an app that hides to the tray on close
    also refuses to quit. This installs an app delegate that calls `on_quit`
    instead. The red close button (``windowShouldClose``) is untouched, so it
    still hides to the tray.

    `on_reopen` is called when the Dock icon is clicked with no visible window.
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
                on_quit()
                return NSTerminateNow  # belt-and-suspenders if on_quit returns

            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
                """Restore the window when the Dock icon is clicked.

                With nothing visible (hidden to the tray) macOS would otherwise
                do nothing, leaving the app looking dead.
                """
                if not has_visible:
                    on_reopen()
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
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("[desktop] could not install the macOS quit handler: %s", exc)
