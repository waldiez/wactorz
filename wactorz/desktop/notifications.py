"""Desktop notifications.

macOS uses UNUserNotificationCenter (modern: prompts for permission once, and is
reliable across recent macOS), falling back to the legacy NSUserNotification if
that's unavailable (dev runs / older macOS). Both deliver on the main thread —
notifications are often fired from worker threads and AppKit/UN delivery off the
main thread silently no-ops. Other platforms use plyer.
"""
from __future__ import annotations

import sys

from wactorz.desktop.config import APP_NAME

# UNAuthorizationOptions: Alert(4) | Sound(2).
_UN_AUTH = 4 | 2
# UNNotificationPresentationOptions for willPresent (show even when frontmost):
# Alert(4, older macOS) | Banner(16) | List(32) | Sound(2).
_UN_PRESENT = 4 | 16 | 32 | 2

_un_delegate = None        # retained: center.delegate is a non-owning reference
_un_ready = False          # delegate set + authorization requested
_legacy_delegate = None    # retained delegate for the NSUserNotification fallback


# ── macOS: modern UNUserNotificationCenter ──────────────────────────────────
def _macos_un_center():
    """UNUserNotificationCenter, or None if unavailable (older macOS / not a real
    bundle / framework missing). First call wires the foreground-presentation
    delegate and requests authorization (the one-time permission prompt)."""
    global _un_delegate, _un_ready
    try:
        from UserNotifications import UNUserNotificationCenter

        center = UNUserNotificationCenter.currentNotificationCenter()
        if center is None:
            return None
        if not _un_ready:
            _un_ready = True
            from Foundation import NSObject

            class _Delegate(NSObject):
                def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                    self, center, notification, completion
                ):
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
    try:
        import uuid

        from UserNotifications import UNMutableNotificationContent, UNNotificationRequest

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
    A delegate forces presentation even when frontmost."""
    global _legacy_delegate
    try:
        from Foundation import NSObject, NSUserNotification, NSUserNotificationCenter

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            return
        if _legacy_delegate is None:
            class _PresentAlways(NSObject):
                def userNotificationCenter_shouldPresentNotification_(self, center, note):
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
    worker threads, where delivery silently no-ops)."""
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_deliver_macos, title, body)
    except Exception:
        pass


def request_authorization() -> None:
    """Proactively request macOS notification permission at launch (shows the
    system prompt once). No-op off macOS / when UN is unavailable."""
    if sys.platform != "darwin":
        return
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_macos_un_center)
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    if sys.platform == "darwin":
        _notify_macos_native(title, body)
        return
    try:
        from plyer import notification

        notification.notify(title=title, message=body, app_name=APP_NAME)
    except Exception:
        pass
