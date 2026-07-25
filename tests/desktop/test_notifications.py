"""Desktop notifications.

The macOS backends are exercised against fake PyObjC modules injected into
`sys.modules`, so the delivery rules can be pinned on any host: notifications
are posted from worker threads, so every macOS path must hop to the main thread,
and both centers need a delegate or banners are suppressed while the app is
frontmost.

Everything here is best-effort by contract — `notify()` must never raise, since
callers fire it mid-task.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from wactorz.desktop import notifications

# ── fake PyObjC ─────────────────────────────────────────────────────────────


class _FakeNSObject:
    """Stand-in base giving PyObjC's alloc()/init() construction."""

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class _FakeUNCenter:
    """Records what the UN path does to the notification center."""

    def __init__(self):
        self.delegate = None
        self.auth_options = None
        self.requests = []

    def setDelegate_(self, delegate):  # pylint: disable=invalid-name
        self.delegate = delegate

    def requestAuthorizationWithOptions_completionHandler_(  # pylint: disable=invalid-name
        self, options, handler
    ):
        self.auth_options = options
        handler(True, None)

    def addNotificationRequest_withCompletionHandler_(  # pylint: disable=invalid-name
        self, request, _handler
    ):
        self.requests.append(request)


class _FakeContent(_FakeNSObject):
    def __init__(self):
        self.title = None
        self.body = None

    def setTitle_(self, value):  # pylint: disable=invalid-name
        self.title = value

    def setBody_(self, value):  # pylint: disable=invalid-name
        self.body = value


class _FakeRequestFactory:
    @staticmethod
    def requestWithIdentifier_content_trigger_(ident, content, trigger):  # pylint: disable=invalid-name
        return SimpleNamespace(ident=ident, content=content, trigger=trigger)


class _FakeLegacyCenter:
    def __init__(self):
        self.delegate = None
        self.delivered = []

    def setDelegate_(self, delegate):  # pylint: disable=invalid-name
        self.delegate = delegate

    def deliverNotification_(self, note):  # pylint: disable=invalid-name
        self.delivered.append(note)


class _FakeNote(_FakeNSObject):
    def __init__(self):
        self.title = None
        self.text = None
        self.action_button = None

    def setTitle_(self, value):  # pylint: disable=invalid-name
        self.title = value

    def setInformativeText_(self, value):  # pylint: disable=invalid-name
        self.text = value

    def setHasActionButton_(self, value):  # pylint: disable=invalid-name
        self.action_button = value


def _module(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture(name="reset", autouse=True)
def reset_fixture(monkeypatch):
    """Forget retained delegates between tests (they are module globals)."""
    monkeypatch.setattr(notifications, "_un_delegate", None)
    monkeypatch.setattr(notifications, "_un_ready", False)
    monkeypatch.setattr(notifications, "_legacy_delegate", None)
    yield
    notifications._un_delegate = None
    notifications._un_ready = False
    notifications._legacy_delegate = None


@pytest.fixture(name="macos")
def macos_fixture(monkeypatch):
    """Pretend to be macOS with PyObjC available. Returns the fake objects."""
    un_center = _FakeUNCenter()
    legacy_center = _FakeLegacyCenter()
    deferred = []

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(
        sys.modules,
        "UserNotifications",
        _module(
            "UserNotifications",
            UNUserNotificationCenter=SimpleNamespace(currentNotificationCenter=lambda: un_center),
            UNMutableNotificationContent=_FakeContent,
            UNNotificationRequest=_FakeRequestFactory,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        _module(
            "Foundation",
            NSObject=_FakeNSObject,
            NSUserNotification=_FakeNote,
            NSUserNotificationCenter=SimpleNamespace(
                defaultUserNotificationCenter=lambda: legacy_center
            ),
        ),
    )

    def _call_after(func, *args):
        deferred.append((func, args))
        func(*args)

    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        _module("PyObjCTools", AppHelper=SimpleNamespace(callAfter=_call_after)),
    )
    return SimpleNamespace(un=un_center, legacy=legacy_center, deferred=deferred)


# ── macOS: modern UN center ─────────────────────────────────────────────────


def test_the_un_center_is_wired_once(macos):
    first = notifications._macos_un_center()
    assert first is macos.un
    assert macos.un.delegate is not None
    delegate = macos.un.delegate

    notifications._macos_un_center()
    assert macos.un.delegate is delegate  # not re-wired


def test_authorization_is_requested_for_alerts_and_sound(macos):
    notifications._macos_un_center()
    assert macos.un.auth_options == notifications._UN_AUTH


def test_the_delegate_is_retained(macos):
    # center.delegate is a non-owning reference; without our own the delegate
    # would be collected and banners would stop presenting.
    notifications._macos_un_center()
    assert notifications._un_delegate is macos.un.delegate


def test_the_delegate_presents_banners_while_frontmost(macos):
    notifications._macos_un_center()
    presented = []
    method = getattr(
        macos.un.delegate,
        "userNotificationCenter_willPresentNotification_withCompletionHandler_",
    )
    method(macos.un, object(), presented.append)
    assert presented == [notifications._UN_PRESENT]


def test_no_un_center_when_the_framework_is_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "UserNotifications", None)  # import raises
    assert notifications._macos_un_center() is None


def test_no_un_center_when_the_system_returns_none(macos, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "UserNotifications",
        _module(
            "UserNotifications",
            UNUserNotificationCenter=SimpleNamespace(currentNotificationCenter=lambda: None),
        ),
    )
    assert notifications._macos_un_center() is None


def test_delivering_through_un_posts_a_request(macos):
    assert notifications._deliver_macos_un("Title", "Body") is True
    (request,) = macos.un.requests
    assert (request.content.title, request.content.body) == ("Title", "Body")
    assert request.trigger is None  # deliver now, not on a schedule


def test_each_delivery_gets_its_own_identifier(macos):
    notifications._deliver_macos_un("a", "1")
    notifications._deliver_macos_un("b", "2")
    first, second = macos.un.requests
    assert first.ident != second.ident  # a reused id replaces the previous banner


def test_un_delivery_reports_failure_when_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "UserNotifications", None)
    assert notifications._deliver_macos_un("t", "b") is False


def test_un_delivery_reports_failure_when_the_center_cannot_be_wired(macos, monkeypatch):
    # The framework imports, but authorization/delegate setup gave us nothing —
    # the caller must fall back to the legacy API rather than drop the message.
    monkeypatch.setattr(notifications, "_macos_un_center", lambda: None)
    assert notifications._deliver_macos_un("t", "b") is False
    assert macos.un.requests == []


# ── macOS: legacy fallback ──────────────────────────────────────────────────


def test_the_legacy_path_delivers_a_notification(macos):
    notifications._deliver_macos_legacy("Title", "Body")
    (note,) = macos.legacy.delivered
    assert (note.title, note.text) == ("Title", "Body")
    assert note.action_button is False


def test_the_legacy_delegate_forces_presentation(macos):
    notifications._deliver_macos_legacy("t", "b")
    delegate = macos.legacy.delegate
    assert delegate.userNotificationCenter_shouldPresentNotification_(macos.legacy, object())


def test_the_legacy_delegate_is_created_once(macos):
    notifications._deliver_macos_legacy("t", "b")
    delegate = notifications._legacy_delegate
    notifications._deliver_macos_legacy("t", "b")
    assert notifications._legacy_delegate is delegate


def test_the_legacy_path_is_silent_when_foundation_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "Foundation", None)
    notifications._deliver_macos_legacy("t", "b")  # must not raise


def test_the_legacy_path_is_silent_without_a_center(macos, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        _module(
            "Foundation",
            NSObject=_FakeNSObject,
            NSUserNotification=_FakeNote,
            NSUserNotificationCenter=SimpleNamespace(defaultUserNotificationCenter=lambda: None),
        ),
    )
    notifications._deliver_macos_legacy("t", "b")
    assert macos.legacy.delivered == []


# ── macOS: dispatch ─────────────────────────────────────────────────────────


def test_un_is_preferred_over_the_legacy_api(macos):
    notifications._deliver_macos("t", "b")
    assert len(macos.un.requests) == 1
    assert macos.legacy.delivered == []


def test_the_legacy_api_takes_over_when_un_fails(macos, monkeypatch):
    monkeypatch.setattr(notifications, "_deliver_macos_un", lambda *_a: False)
    notifications._deliver_macos("t", "b")
    assert len(macos.legacy.delivered) == 1


def test_macos_delivery_hops_to_the_main_thread(macos):
    # Callers are worker threads; AppKit delivery off the main thread no-ops.
    notifications._notify_macos_native("t", "b")
    assert macos.deferred == [(notifications._deliver_macos, ("t", "b"))]


def test_macos_delivery_is_silent_without_pyobjc(monkeypatch):
    monkeypatch.setitem(sys.modules, "PyObjCTools", None)
    notifications._notify_macos_native("t", "b")  # must not raise


# ── authorization at launch ─────────────────────────────────────────────────


def test_authorization_is_requested_at_launch(macos):
    notifications.request_authorization()
    assert macos.un.auth_options == notifications._UN_AUTH


def test_authorization_is_a_no_op_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "PyObjCTools", None)
    notifications.request_authorization()  # must not raise or import anything


def test_authorization_is_silent_without_pyobjc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "PyObjCTools", None)
    notifications.request_authorization()  # must not raise


# ── the bundle's library path ───────────────────────────────────────────────


def test_the_bundles_library_path_is_not_passed_to_host_tools(monkeypatch):
    # notify-send is a host binary: the bundle's libs are often ABI-mismatched.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/appimage/usr/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in notifications._host_env()


def test_the_original_library_path_is_restored(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/appimage/usr/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/x86_64-linux-gnu")
    env = notifications._host_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/x86_64-linux-gnu"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_an_unbundled_environment_is_passed_through(monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    assert notifications._host_env()["SOME_OTHER_VAR"] == "kept"


# ── Linux: notify-send ──────────────────────────────────────────────────────


@pytest.fixture(name="run_calls")
def run_calls_fixture(monkeypatch, tmp_path):
    """Capture subprocess.run and point APP_ICON at a missing file."""
    calls = []
    monkeypatch.setattr(notifications.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(notifications, "APP_ICON", tmp_path / "icon.png")
    return calls


def test_notify_send_is_called_with_the_app_name(run_calls):
    assert notifications._notify_linux("Title", "Body") is True
    cmd, _kwargs = run_calls[0]
    assert cmd == ["notify-send", "-a", notifications.APP_NAME, "Title", "Body"]


def test_the_icon_is_passed_when_it_is_bundled(run_calls, monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    monkeypatch.setattr(notifications, "APP_ICON", icon)
    notifications._notify_linux("Title", "Body")
    cmd, _kwargs = run_calls[0]
    assert cmd[3:5] == ["-i", str(icon)]


def test_notify_send_output_is_silenced(run_calls):
    # Without a notification daemon it spews a GDBus ServiceUnknown error.
    notifications._notify_linux("t", "b")
    _cmd, kwargs = run_calls[0]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["check"] is False


def test_notify_send_runs_with_the_host_library_path(run_calls, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/appimage/usr/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    notifications._notify_linux("t", "b")
    _cmd, kwargs = run_calls[0]
    assert "LD_LIBRARY_PATH" not in kwargs["env"]


def test_a_missing_notify_send_reports_failure(monkeypatch):
    monkeypatch.setattr(
        notifications.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("notify-send")),
    )
    assert notifications._notify_linux("t", "b") is False


# ── notify(): backend selection ─────────────────────────────────────────────


@pytest.fixture(name="plyer_calls")
def plyer_calls_fixture(monkeypatch, tmp_path):
    """Install a fake plyer and point APP_ICON at a missing file."""
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "plyer",
        _module("plyer", notification=SimpleNamespace(notify=lambda **kw: calls.append(kw))),
    )
    monkeypatch.setattr(notifications, "APP_ICON", tmp_path / "icon.png")
    return calls


def test_macos_uses_the_native_backend(monkeypatch, plyer_calls):
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(notifications, "_notify_macos_native", lambda t, b: seen.append((t, b)))
    notifications.notify("t", "b")
    assert seen == [("t", "b")]
    assert plyer_calls == []


def test_linux_uses_notify_send(monkeypatch, plyer_calls):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(notifications, "_notify_linux", lambda *_a: True)
    notifications.notify("t", "b")
    assert plyer_calls == []


def test_linux_falls_back_to_plyer_when_notify_send_fails(monkeypatch, plyer_calls):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(notifications, "_notify_linux", lambda *_a: False)
    notifications.notify("Title", "Body")
    assert plyer_calls[0]["title"] == "Title"
    assert plyer_calls[0]["message"] == "Body"


def test_windows_uses_plyer(monkeypatch, plyer_calls):
    monkeypatch.setattr(sys, "platform", "win32")
    notifications.notify("Title", "Body")
    assert plyer_calls[0]["app_name"] == notifications.APP_NAME
    assert plyer_calls[0]["app_icon"] == ""  # no bundled icon → no icon


def test_plyer_gets_the_bundled_icon_when_there_is_one(monkeypatch, plyer_calls, tmp_path):
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(notifications, "APP_ICON", icon)
    notifications.notify("t", "b")
    assert plyer_calls[0]["app_icon"] == str(icon)


def test_notify_is_silent_with_no_backend_at_all(monkeypatch):
    # Callers fire this mid-task; a missing notifier must never propagate.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "plyer", None)
    notifications.notify("t", "b")  # must not raise
