"""Desktop notifications.

macOS posts NSUserNotification via pyobjc (already provided by pywebview's Cocoa
backend) with a delegate that forces the banner to show even when we are
frontmost. Other platforms use plyer.
"""
from __future__ import annotations

import sys

from wactorz.desktop.config import APP_NAME

# Retained because NSUserNotificationCenter.delegate is a non-owning reference —
# if the Python delegate is collected, foreground presentation stops working.
_macos_notif_delegate = None


def _deliver_macos_notification(title: str, body: str) -> None:
    """Build + deliver the NSUserNotification. MUST run on the main thread (see
    _notify_macos_native). Installs a delegate that forces the banner to show
    even when Wactorz is frontmost; macOS suppresses it for the active app."""
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
        # NSUserNotification shows an action button ("Show") by default; we don't
        # handle activation, so drop it rather than present a dead button.
        note.setHasActionButton_(False)
        center.deliverNotification_(note)
    except Exception:
        pass


def _notify_macos_native(title: str, body: str) -> None:
    """Post a notification, hopping to the main thread. Notifications are often
    fired from worker threads (update check, backend wait), and AppKit delivery
    off the main thread silently no-ops — so marshal it onto the main run loop."""
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_deliver_macos_notification, title, body)
    except Exception:
        pass


def notify(title: str, body: str) -> None:
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
