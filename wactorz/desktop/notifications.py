"""Desktop notifications.

macOS uses UNUserNotificationCenter (modern: prompts for permission once, and is
reliable across recent macOS), falling back to the legacy NSUserNotification if
that's unavailable (dev runs / older macOS). Both deliver on the main thread —
notifications are often fired from worker threads and AppKit/UN delivery off the
main thread silently no-ops. Other platforms use plyer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

from wactorz.desktop.config import APP_ICON, APP_NAME

# UNAuthorizationOptions: Alert(4) | Sound(2).
_UN_AUTH = 4 | 2
# UNNotificationPresentationOptions for willPresent (show even when frontmost):
# Alert(4, older macOS) | Banner(16) | List(32) | Sound(2).
_UN_PRESENT = 4 | 16 | 32 | 2

_un_delegate = None  # retained: center.delegate is a non-owning reference
_un_ready = False  # delegate set + authorization requested
_legacy_delegate = None  # retained delegate for the NSUserNotification fallback


# ── macOS: modern UNUserNotificationCenter ──────────────────────────────────
def _macos_un_center():
    """UNUserNotificationCenter, or None if unavailable (older macOS / not a real
    bundle / framework missing). First call wires the foreground-presentation
    delegate and requests authorization (the one-time permission prompt).
    """
    # pylint: disable=import-error
    global _un_delegate, _un_ready
    try:
        from UserNotifications import (  # pyright: ignore[reportMissingImports]
            UNUserNotificationCenter,
        )

        center = UNUserNotificationCenter.currentNotificationCenter()
        if center is None:
            return None
        if not _un_ready:
            _un_ready = True
            from Foundation import NSObject  # pyright: ignore[reportMissingImports]

            class _Delegate(NSObject):  # pylint: disable=too-few-public-methods
                """Lets banners appear while the app itself is frontmost."""

                def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                    self, center, notification, completion
                ):
                    """Present the banner even when the app has focus.

                    macOS hides notifications from the foreground app unless
                    its delegate asks for them explicitly.
                    """
                    completion(_UN_PRESENT)

            _un_delegate = _Delegate.alloc().init()
            center.setDelegate_(_un_delegate)
            center.requestAuthorizationWithOptions_completionHandler_(
                _UN_AUTH, lambda granted, error: None
            )
        return center
    except Exception:
        return None


def _deliver_macos_un(title: str, body: str) -> bool:
    """Deliver via UNUserNotificationCenter. Returns True only if posted."""
    # pylint: disable=import-error
    try:
        from UserNotifications import (  # pyright: ignore[reportMissingImports]
            UNMutableNotificationContent,
            UNNotificationRequest,
        )

        center = _macos_un_center()
        if center is None:
            return False
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None
        )
        center.addNotificationRequest_withCompletionHandler_(request, None)
        return True
    except Exception:
        return False


# ── macOS: legacy NSUserNotification fallback ───────────────────────────────
def _deliver_macos_legacy(title: str, body: str) -> None:
    """Deprecated NSUserNotification path — used only when UN is unavailable.
    A delegate forces presentation even when frontmost.
    """
    # pylint: disable=import-error
    global _legacy_delegate
    try:
        from Foundation import (  # pyright: ignore[reportMissingImports]
            NSObject,
            NSUserNotification,
            NSUserNotificationCenter,
        )

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            return
        if _legacy_delegate is None:

            class _PresentAlways(NSObject):  # pylint: disable=too-few-public-methods
                """Legacy-API counterpart of the UN delegate above."""

                def userNotificationCenter_shouldPresentNotification_(self, center, note):
                    """Present the banner even when the app has focus."""
                    return True

            _legacy_delegate = _PresentAlways.alloc().init()
        center.setDelegate_(_legacy_delegate)
        note = NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(body)
        note.setHasActionButton_(False)
        center.deliverNotification_(note)
    except Exception:
        pass


def _deliver_macos(title: str, body: str) -> None:
    """Runs on the main thread: prefer UN, fall back to legacy."""
    if not _deliver_macos_un(title, body):
        _deliver_macos_legacy(title, body)


def _notify_macos_native(title: str, body: str) -> None:
    """Post a macOS notification, hopping to the main thread (callers are often
    worker threads, where delivery silently no-ops).
    """
    # pylint: disable=import-error
    try:
        from PyObjCTools import AppHelper  # pyright: ignore[reportMissingImports]

        AppHelper.callAfter(_deliver_macos, title, body)
    except Exception:
        pass


def request_authorization() -> None:
    """Proactively request macOS notification permission at launch (shows the
    system prompt once). No-op off macOS / when UN is unavailable.
    """
    if sys.platform != "darwin":
        return
    # pylint: disable=import-error
    try:
        from PyObjCTools import AppHelper  # pyright: ignore[reportMissingImports]

        AppHelper.callAfter(_macos_un_center)
    except Exception:
        pass


def _host_env() -> dict:
    """Environment for running a host binary from inside the (AppImage/PyInstaller)
    bundle. The bundle sets LD_LIBRARY_PATH to its own libs; a host tool like
    notify-send must NOT inherit that or it loads the bundle's (possibly older,
    ABI-mismatched) libs and fails. PyInstaller stashes the real value in
    LD_LIBRARY_PATH_ORIG — restore it (or drop the override).
    """
    env = os.environ.copy()
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _notify_linux(title: str, body: str) -> bool:
    """Use notify-send (libnotify) directly. Avoids plyer's hard dbus-python
    dependency (a C extension we don't bundle). notify-send ships on essentially
    every Linux desktop. Returns True if the call ran.
    """
    try:
        cmd = ["notify-send", "-a", APP_NAME]
        if APP_ICON.exists():
            cmd += ["-i", str(APP_ICON)]
        cmd += [title, body]
        # Run with the host's library path (see _host_env) and silence output:
        # with no notification daemon, notify-send spews a GDBus ServiceUnknown.
        subprocess.run(
            cmd, check=False, env=_host_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False


def notify(title: str, body: str) -> None:
    """Show an OS notification using the best backend for this platform.

    Best-effort: with no notifier available the call is silently dropped —
    a missing notification must never break the caller mid-task.
    """
    if sys.platform == "darwin":
        _notify_macos_native(title, body)
        return
    if sys.platform.startswith("linux") and _notify_linux(title, body):
        return
    try:
        from plyer import notification

        # Windows path (and Linux fallback). app_icon gives the toast its icon;
        # APP_ICON resolves to the bundled per-platform icon.
        icon = str(APP_ICON) if APP_ICON.exists() else ""
        notification.notify(  # pyright: ignore[reportOptionalCall]
            title=title,
            message=body,
            app_name=APP_NAME,
            app_icon=icon,
        )
    except Exception:
        pass
