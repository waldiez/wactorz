"""Per-OS integration hooks for the desktop shell.

Both hooks are unreachable on the machine that usually runs this suite, so the
platform is faked: `sys.platform` / `os.name` for the branch, and the native
modules via `monkeypatch.setitem(sys.modules, …)` so they are restored after
each test (never a raw `sys.modules[...] = ` assignment, which leaks into every
later test).

What matters here is the contract the shell depends on: the hooks are silent
no-ops off their platform, they never raise, and the macOS delegate routes
Dock ▸ Quit and Dock-icon-click to the callbacks it was handed.
"""

# pylint: disable=missing-function-docstring

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from wactorz.desktop import platform_hooks
from wactorz.desktop.config import APP_ID


@pytest.fixture(name="no_delegate", autouse=True)
def no_delegate_fixture():
    """Drop any delegate a previous test installed."""
    platform_hooks._macos_app_delegate = None
    yield
    platform_hooks._macos_app_delegate = None


# ── Windows: AppUserModelID ─────────────────────────────────────────────────


def _fake_ctypes(recorder: list, exc: Exception | None = None) -> ModuleType:
    mod = ModuleType("ctypes")

    def _set_id(app_id):
        if exc is not None:
            raise exc
        recorder.append(app_id)

    mod.windll = SimpleNamespace(  # type: ignore[attr-defined]
        shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=_set_id)
    )
    return mod


def test_app_user_model_id_is_a_noop_off_windows(monkeypatch):
    calls: list = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(calls))
    platform_hooks.set_app_user_model_id()
    assert not calls


def test_app_user_model_id_is_set_on_windows(monkeypatch):
    calls: list = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes(calls))
    platform_hooks.set_app_user_model_id()
    assert calls == [APP_ID]


def test_app_user_model_id_swallows_a_failing_shell_call(monkeypatch):
    # A cosmetic taskbar grouping is never worth failing the launch over.
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", _fake_ctypes([], exc=OSError("nope")))
    platform_hooks.set_app_user_model_id()  # must not raise


# ── macOS: quit handler ─────────────────────────────────────────────────────


class _FakeNSObject:
    """Stand-in for Foundation.NSObject's alloc()/init() construction."""

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def _install_fake_appkit(monkeypatch, *, run_callafter: bool = True) -> SimpleNamespace:
    """Fake AppKit/Foundation/PyObjCTools; return what the delegate is set on."""
    app = SimpleNamespace(delegate=None)
    app.setDelegate_ = lambda d: setattr(app, "delegate", d)

    appkit = ModuleType("AppKit")
    appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: app)  # type: ignore[attr-defined]
    appkit.NSTerminateNow = object()  # type: ignore[attr-defined]

    foundation = ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject  # type: ignore[attr-defined]

    helper = ModuleType("PyObjCTools")
    # AppHelper.callAfter hops to the main thread; run it inline so the test can
    # observe the delegate actually being installed.
    helper.AppHelper = SimpleNamespace(  # type: ignore[attr-defined]
        callAfter=(lambda fn: fn()) if run_callafter else (lambda fn: None)
    )

    for name, mod in (("AppKit", appkit), ("Foundation", foundation), ("PyObjCTools", helper)):
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(app=app, terminate_now=appkit.NSTerminateNow)


def test_quit_handler_is_a_noop_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    quits: list = []
    platform_hooks.install_quit_handler(on_quit=lambda: quits.append(1), on_reopen=lambda: None)
    assert platform_hooks._macos_app_delegate is None
    assert not quits


def test_quit_handler_installs_the_delegate_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fakes = _install_fake_appkit(monkeypatch)
    platform_hooks.install_quit_handler(on_quit=lambda: None, on_reopen=lambda: None)
    # Retained module-side: AppKit holds the delegate weakly, so a local would be
    # collected and the app would silently fall back to pywebview's delegate.
    assert platform_hooks._macos_app_delegate is not None
    assert fakes.app.delegate is platform_hooks._macos_app_delegate


def test_dock_quit_runs_the_shutdown_callback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fakes = _install_fake_appkit(monkeypatch)
    quits: list = []
    platform_hooks.install_quit_handler(on_quit=lambda: quits.append(1), on_reopen=lambda: None)

    result = fakes.app.delegate.applicationShouldTerminate_(None)
    assert quits == [1]
    assert result is fakes.terminate_now


def test_dock_icon_click_reopens_only_when_nothing_is_visible(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fakes = _install_fake_appkit(monkeypatch)
    reopens: list = []
    platform_hooks.install_quit_handler(on_quit=lambda: None, on_reopen=lambda: reopens.append(1))
    delegate = fakes.app.delegate

    assert delegate.applicationShouldHandleReopen_hasVisibleWindows_(None, False) is True
    assert reopens == [1]  # hidden to the tray → bring it back
    assert delegate.applicationShouldHandleReopen_hasVisibleWindows_(None, True) is True
    assert reopens == [1]  # already visible → nothing to do


def test_delegate_declares_secure_state_restoration(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fakes = _install_fake_appkit(monkeypatch)
    platform_hooks.install_quit_handler(on_quit=lambda: None, on_reopen=lambda: None)
    assert fakes.app.delegate.applicationSupportsSecureRestorableState_(None) is True


def test_quit_handler_swallows_a_missing_objc_bridge(monkeypatch):
    # A source install on macOS without pyobjc: the app still runs, just without
    # the Dock-Quit fix.
    monkeypatch.setattr(sys, "platform", "darwin")
    broken = ModuleType("AppKit")  # no NSApplication attribute
    monkeypatch.setitem(sys.modules, "AppKit", broken)
    platform_hooks.install_quit_handler(on_quit=lambda: None, on_reopen=lambda: None)
    assert platform_hooks._macos_app_delegate is None
