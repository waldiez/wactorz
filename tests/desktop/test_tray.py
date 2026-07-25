"""System-tray menu.

Both builders run against fake toolkits injected into `sys.modules` — a real
QSystemTrayIcon needs a display and a running event loop, and pystray's native
backends need macOS/Windows. What is pinned here is the wiring the shell relies
on: every menu entry reaches its hook, the checkable entries re-read state
instead of trusting their own checkmark, and an unavailable tray degrades to
None rather than raising (the app must still start without a tray).
"""

# pylint: disable=missing-function-docstring,too-few-public-methods
# pyright: reportOptionalMemberAccess=false,reportAttributeAccessIssue=false
# pyright: reportCallIssue=false,reportOptionalCall=false

import sys
from types import ModuleType, SimpleNamespace

import pytest

from wactorz.desktop import tray
from wactorz.desktop.config import APP_ID, APP_NAME

# ── hooks ───────────────────────────────────────────────────────────────────


@pytest.fixture(name="state")
def state_fixture():
    """Mutable shell state plus a TrayHooks bound to it."""
    st = SimpleNamespace(
        calls=[],
        autostart=False,
        auto_update=False,
        pending="",
        autostart_writable=True,
    )

    def _set_autostart(value):
        st.calls.append(("set_autostart", value))
        if st.autostart_writable:
            st.autostart = value

    def _set_auto_update(value):
        st.calls.append(("set_auto_update", value))
        st.auto_update = value

    st.hooks = tray.TrayHooks(
        on_toggle=lambda: st.calls.append("toggle"),
        on_configure=lambda: st.calls.append("configure"),
        on_check_updates=lambda: st.calls.append("check_updates"),
        on_quit=lambda: st.calls.append("quit"),
        autostart_enabled=lambda: st.autostart,
        set_autostart=_set_autostart,
        auto_update_enabled=lambda: st.auto_update,
        set_auto_update=_set_auto_update,
        pending_update_version=lambda: st.pending,
        open_download=lambda: st.calls.append("download"),
    )
    return st


# ── fake Qt ─────────────────────────────────────────────────────────────────


class _Signal:
    """Minimal Qt signal: connect handlers, emit to call them."""

    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def emit(self, *args):
        for handler in self.handlers:
            handler(*args)


class _QAction:
    def __init__(self, text, _parent=None):
        self.text = text
        self.checkable = False
        self.checked = False
        self.visible = True
        self.triggered = _Signal()

    def setText(self, text):  # pylint: disable=invalid-name
        self.text = text

    def setCheckable(self, value):  # pylint: disable=invalid-name
        self.checkable = value

    def setChecked(self, value):  # pylint: disable=invalid-name
        self.checked = value

    def setVisible(self, value):  # pylint: disable=invalid-name
        self.visible = value


class _QMenu:
    SEPARATOR = object()

    def __init__(self):
        self.items = []
        self.aboutToShow = _Signal()

    def addAction(self, action):  # pylint: disable=invalid-name
        self.items.append(action)

    def addSeparator(self):  # pylint: disable=invalid-name
        self.items.append(self.SEPARATOR)


class _QApplication:
    current = None

    def __init__(self, _argv=None):
        self.identity = {}
        _QApplication.current = self

    @classmethod
    def instance(cls):
        return cls.current

    def setApplicationName(self, value):  # pylint: disable=invalid-name
        self.identity["name"] = value

    def setOrganizationName(self, value):  # pylint: disable=invalid-name
        self.identity["org"] = value

    def setOrganizationDomain(self, value):  # pylint: disable=invalid-name
        self.identity["domain"] = value

    def setDesktopFileName(self, value):  # pylint: disable=invalid-name
        self.identity["desktop_file"] = value


class _QSystemTrayIcon:
    available = True
    ActivationReason = SimpleNamespace(Trigger="trigger", DoubleClick="double")

    def __init__(self, icon):
        self.icon = icon
        self.menu = None
        self.tooltip = None
        self.shown = False
        self.activated = _Signal()

    @staticmethod
    def isSystemTrayAvailable():  # pylint: disable=invalid-name
        return _QSystemTrayIcon.available

    def setContextMenu(self, menu):  # pylint: disable=invalid-name
        self.menu = menu

    def setToolTip(self, text):  # pylint: disable=invalid-name
        self.tooltip = text

    def show(self):
        self.shown = True


def _module(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture(name="qt", autouse=True)
def qt_fixture(monkeypatch):
    """Install the fake PySide6 and reset its module-level singletons."""
    _QApplication.current = None
    _QSystemTrayIcon.available = True
    pyside = _module("PySide6")
    qtgui = _module("PySide6.QtGui", QAction=_QAction, QIcon=str)
    qtwidgets = _module(
        "PySide6.QtWidgets",
        QApplication=_QApplication,
        QMenu=_QMenu,
        QSystemTrayIcon=_QSystemTrayIcon,
    )
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qtwidgets)
    yield
    _QApplication.current = None


def _action(qt_tray, label):
    """The menu action whose label starts with `label`."""
    for item in qt_tray.menu.items:
        if isinstance(item, _QAction) and item.text.startswith(label):
            return item
    raise AssertionError(f"no action labelled {label!r}")


# ── Qt tray: construction ───────────────────────────────────────────────────


def test_no_qt_tray_without_pyside(state, monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", None)
    assert tray.build_qt_tray(state.hooks) is None


def test_no_qt_tray_without_a_tray_area(state):
    # A desktop with no status-notifier host: the app must still start.
    _QSystemTrayIcon.available = False
    assert tray.build_qt_tray(state.hooks) is None


def test_the_tray_is_shown_with_a_tooltip(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    assert qt_tray.shown is True
    assert qt_tray.tooltip == APP_NAME


def test_the_app_identity_matches_the_installed_desktop_file(state):
    # This is what binds the window to its taskbar icon and grouping.
    tray.build_qt_tray(state.hooks)
    assert _QApplication.current.identity["desktop_file"] == APP_ID
    assert _QApplication.current.identity["name"] == APP_NAME


def test_an_existing_qapplication_is_reused(state):
    existing = _QApplication()
    tray.build_qt_tray(state.hooks)
    assert _QApplication.current is existing  # pywebview owns the real one


# ── Qt tray: menu wiring ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Show / Hide", "toggle"),
        ("Configure", "configure"),
        ("Check for Updates", "check_updates"),
        ("Quit", "quit"),
    ],
)
def test_each_entry_reaches_its_hook(state, label, expected):
    qt_tray = tray.build_qt_tray(state.hooks)
    _action(qt_tray, label).triggered.emit()
    assert state.calls == [expected]


def test_clicking_the_icon_toggles_the_window(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    qt_tray.activated.emit(_QSystemTrayIcon.ActivationReason.Trigger)
    assert state.calls == ["toggle"]


def test_other_activation_reasons_are_ignored(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    qt_tray.activated.emit(_QSystemTrayIcon.ActivationReason.DoubleClick)
    assert state.calls == []


def test_the_menu_is_grouped_by_separators(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    labels = [i.text if isinstance(i, _QAction) else "---" for i in qt_tray.menu.items]
    assert labels == [
        "Show / Hide",
        "Configure...",
        "Check for Updates...",
        "Download update",
        "---",
        "Start at login",
        "Check for updates automatically",
        "---",
        "Quit Wactorz",
    ]


# ── Qt tray: the download entry ─────────────────────────────────────────────


def test_the_download_entry_is_hidden_without_an_update(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    assert _action(qt_tray, "Download").visible is False


def test_the_download_entry_appears_when_an_update_lands(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    download = _action(qt_tray, "Download")

    state.pending = "0.6.0"
    qt_tray.menu.aboutToShow.emit()  # re-evaluated each time the menu opens
    assert download.visible is True
    assert download.text == "Download v0.6.0..."


def test_the_download_entry_opens_the_release(state):
    state.pending = "0.6.0"
    qt_tray = tray.build_qt_tray(state.hooks)
    _action(qt_tray, "Download").triggered.emit()
    assert state.calls == ["download"]


# ── Qt tray: checkable entries ──────────────────────────────────────────────


def test_checkable_entries_start_from_the_current_state(state):
    state.autostart = True
    qt_tray = tray.build_qt_tray(state.hooks)
    assert _action(qt_tray, "Start at login").checked is True
    assert _action(qt_tray, "Check for updates").checked is False


def test_toggling_an_entry_writes_the_new_value(state):
    qt_tray = tray.build_qt_tray(state.hooks)
    _action(qt_tray, "Start at login").triggered.emit(True)
    assert ("set_autostart", True) in state.calls
    assert state.autostart is True


def test_a_failed_write_re_syncs_the_checkmark(state):
    # The OS can refuse the autostart entry; the menu must not lie about it.
    state.autostart_writable = False
    qt_tray = tray.build_qt_tray(state.hooks)
    action = _action(qt_tray, "Start at login")
    action.triggered.emit(True)
    assert action.checked is False


# ── pystray ─────────────────────────────────────────────────────────────────


class _MenuItem:
    def __init__(self, text, action=None, checked=None, visible=True, default=False):
        self.text = text
        self.action = action
        self.checked = checked
        self.visible = visible
        self.default = default

    @property
    def label(self):
        """The rendered label (pystray calls a callable text)."""
        return self.text(self) if callable(self.text) else self.text


class _Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class _Icon:
    fail = False

    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.detached = False
        self.updates = 0

    def run_detached(self):
        if _Icon.fail:
            raise RuntimeError("main thread only")
        self.detached = True

    def update_menu(self):
        self.updates += 1


@pytest.fixture(name="pystray", autouse=True)
def pystray_fixture(monkeypatch, tmp_path):
    """Install fake pystray + PIL, with a readable bundled icon."""
    _Icon.fail = False
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "icon.png").write_bytes(b"png")
    monkeypatch.setattr(tray, "_ASSETS", assets)
    monkeypatch.setitem(
        sys.modules,
        "pystray",
        _module("pystray", Menu=_Menu, MenuItem=_MenuItem, Icon=_Icon),
    )
    monkeypatch.setitem(sys.modules, "PIL", _module("PIL"))
    monkeypatch.setitem(
        sys.modules,
        "PIL.Image",
        _module("PIL.Image", open=lambda path: SimpleNamespace(path=path)),
    )


def _item(icon, label):
    for item in icon.menu.items:
        if isinstance(item, _MenuItem) and item.label.startswith(label):
            return item
    raise AssertionError(f"no item labelled {label!r}")


def test_no_pystray_tray_without_the_library(state, monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray.build_pystray_tray(state.hooks) is None


def test_no_pystray_tray_without_a_readable_icon(state, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "PIL.Image",
        _module("PIL.Image", open=lambda _p: (_ for _ in ()).throw(OSError("no icon"))),
    )
    assert tray.build_pystray_tray(state.hooks) is None


def test_the_pystray_icon_runs_on_its_own_thread(state):
    icon = tray.build_pystray_tray(state.hooks)
    assert icon.detached is True
    assert icon.title == APP_NAME


def test_a_tray_that_cannot_run_degrades_to_none(state):
    # e.g. macOS' main-thread-only limitation — the app still starts.
    _Icon.fail = True
    assert tray.build_pystray_tray(state.hooks) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Show / Hide", "toggle"),
        ("Configure", "configure"),
        ("Check for Updates...", "check_updates"),
        ("Quit", "quit"),
    ],
)
def test_each_pystray_entry_reaches_its_hook(state, label, expected):
    icon = tray.build_pystray_tray(state.hooks)
    _item(icon, label).action(icon, None)
    assert state.calls == [expected]


def test_show_hide_is_the_default_click_action(state):
    icon = tray.build_pystray_tray(state.hooks)
    assert _item(icon, "Show / Hide").default is True


def test_the_pystray_download_entry_tracks_the_pending_update(state):
    icon = tray.build_pystray_tray(state.hooks)
    download = _item(icon, "Download")
    assert download.visible(download) is False

    state.pending = "0.6.0"
    assert download.visible(download) is True
    assert download.label == "Download v0.6.0..."


def test_the_pystray_download_entry_opens_the_release(state):
    state.pending = "0.6.0"
    icon = tray.build_pystray_tray(state.hooks)
    _item(icon, "Download").action(icon, None)
    assert state.calls == ["download"]


def test_pystray_checkmarks_read_live_state(state):
    icon = tray.build_pystray_tray(state.hooks)
    autostart = _item(icon, "Start at login")
    assert autostart.checked(autostart) is False
    state.autostart = True
    assert autostart.checked(autostart) is True


def test_a_pystray_toggle_flips_the_value_and_re_renders(state):
    icon = tray.build_pystray_tray(state.hooks)
    autostart = _item(icon, "Start at login")
    autostart.action(icon, autostart)
    assert state.autostart is True
    assert icon.updates == 1  # the checkmark is redrawn from the new state

    autostart.action(icon, autostart)
    assert state.autostart is False


def test_the_pystray_auto_update_toggle_flips_its_own_value(state):
    icon = tray.build_pystray_tray(state.hooks)
    auto = _item(icon, "Check for updates automatically")
    auto.action(icon, auto)
    assert state.auto_update is True
    assert state.autostart is False  # untouched
