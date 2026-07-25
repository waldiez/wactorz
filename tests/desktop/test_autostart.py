"""Run-at-login toggle.

Every platform branch is exercised here — `sys.platform` / `os.name` are patched
and Windows gets a fake `winreg` — because the artifact each one writes is what
the OS reads at login, and a wrong Exec line yields an entry that silently fails
to start the app.

The paths are redirected at tmp dirs, so nothing here touches the real
LaunchAgents / autostart directory.
"""

# pylint: disable=missing-function-docstring

import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from wactorz.desktop import autostart
from wactorz.desktop.config import APP_ID, APP_NAME


@pytest.fixture(name="paths", autouse=True)
def paths_fixture(tmp_path, monkeypatch):
    """Redirect both file-backed entries at tmp paths (parents absent).

    XDG is pinned at empty dirs as well: a real installed launcher on the host
    would otherwise be picked up by the copy-the-launcher path, and the result
    would depend on whether the developer has the app installed.
    """
    plist = tmp_path / "LaunchAgents" / f"{APP_ID}.plist"
    desktop = tmp_path / "autostart" / f"{APP_ID}.desktop"
    monkeypatch.setattr(autostart, "_PLIST", plist)
    monkeypatch.setattr(autostart, "_DESKTOP", desktop)
    monkeypatch.setattr(autostart, "FROZEN", False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-data-home"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-data-dirs"))
    return SimpleNamespace(plist=plist, desktop=desktop)


def _pretend_os_name(monkeypatch, name):
    """Swap os.name *for this module only*.

    Patching the real os.name is not safe: pathlib dispatches Path() on it, and
    a WindowsPath cannot be instantiated on a posix host.
    """
    monkeypatch.setattr(autostart, "os", SimpleNamespace(name=name, environ=os.environ))


@pytest.fixture(name="linux", autouse=True)
def linux_fixture(monkeypatch):
    """Default every test to the Linux branch; opt out by re-patching."""
    monkeypatch.setattr(sys, "platform", "linux")
    _pretend_os_name(monkeypatch, "posix")


class _FakeKey:
    """A registry key that is also its own context manager."""

    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeWinreg:
    """Enough of winreg to exercise the Run-key branch."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self, values=None, open_error=None):
        self.values = {} if values is None else values
        self._open_error = open_error

    def OpenKey(self, _root, _sub, _res=0, _access=0):  # pylint: disable=invalid-name
        if self._open_error is not None:
            raise self._open_error
        return _FakeKey(self.values)

    def QueryValueEx(self, key, name):  # pylint: disable=invalid-name
        if name not in key.values:
            raise FileNotFoundError(name)  # what the real winreg raises
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _res, _typ, value):  # pylint: disable=invalid-name
        key.values[name] = value

    def DeleteValue(self, key, name):  # pylint: disable=invalid-name
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


@pytest.fixture(name="windows")
def windows_fixture(monkeypatch):
    """Switch to the Windows branch with an in-memory registry."""

    def _install(values=None, open_error=None):
        fake = _FakeWinreg(values, open_error)
        monkeypatch.setattr(sys, "platform", "win32")
        _pretend_os_name(monkeypatch, "nt")
        monkeypatch.setitem(sys.modules, "winreg", fake)
        return fake

    return _install


@pytest.fixture(name="macos")
def macos_fixture(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


# ── the relaunch command ────────────────────────────────────────────────────


def test_launch_argv_runs_the_module_from_source():
    assert autostart._launch_argv() == [sys.executable, "-m", "wactorz.desktop.app"]


def test_launch_argv_uses_the_frozen_executable(monkeypatch):
    monkeypatch.setattr(autostart, "FROZEN", True)
    assert autostart._launch_argv() == [sys.executable]


def test_launch_argv_prefers_the_appimage_path(monkeypatch):
    # sys.executable inside an AppImage is a transient mount that will not exist
    # at the next login — $APPIMAGE is the stable one.
    monkeypatch.setenv("APPIMAGE", "/home/u/Apps/Wactorz.AppImage")
    monkeypatch.setattr(autostart, "FROZEN", True)
    assert autostart._launch_argv() == ["/home/u/Apps/Wactorz.AppImage"]


# ── is_enabled ──────────────────────────────────────────────────────────────


def test_linux_is_enabled_follows_the_desktop_file(paths):
    assert autostart.is_enabled() is False
    paths.desktop.parent.mkdir(parents=True)
    paths.desktop.write_text("[Desktop Entry]\n")
    assert autostart.is_enabled() is True


def test_macos_is_enabled_follows_the_plist(paths, macos):
    assert autostart.is_enabled() is False
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_text("<plist/>")
    assert autostart.is_enabled() is True


def test_windows_is_enabled_follows_the_run_value(windows):
    windows()
    assert autostart.is_enabled() is False
    windows({APP_NAME: "C:\\wactorz.exe"})
    assert autostart.is_enabled() is True


def test_an_unreadable_registry_counts_as_disabled(windows):
    windows(open_error=OSError("access denied"))
    assert autostart.is_enabled() is False


# ── set_enabled: linux ──────────────────────────────────────────────────────


def test_enabling_on_linux_writes_an_autostart_entry(paths):
    assert autostart.set_enabled(True) is True
    body = paths.desktop.read_text()
    assert "X-GNOME-Autostart-enabled=true" in body
    assert f"Exec={shlex.join(autostart._launch_argv())}" in body
    assert f"StartupWMClass={APP_ID}" in body
    assert autostart.is_enabled() is True


def test_disabling_on_linux_removes_the_entry(paths):
    autostart.set_enabled(True)
    assert autostart.set_enabled(False) is True
    assert not paths.desktop.exists()


def test_disabling_when_already_disabled_is_fine(paths):
    assert autostart.set_enabled(False) is True  # missing_ok


def test_the_installed_launcher_is_copied_when_there_is_one(paths, tmp_path, monkeypatch):
    # Keeps Exec/Icon/StartupWMClass in sync with packaging instead of guessing.
    share = tmp_path / "share"
    installed = share / "applications" / f"{APP_ID}.desktop"
    installed.parent.mkdir(parents=True)
    installed.write_text("[Desktop Entry]\nExec=/usr/bin/wactorz\nIcon=wactorz\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(share))

    autostart.set_enabled(True)
    body = paths.desktop.read_text()
    assert "Exec=/usr/bin/wactorz" in body
    assert body.endswith("X-GNOME-Autostart-enabled=true\n")


def test_a_launcher_that_is_already_marked_is_not_marked_twice(paths, tmp_path, monkeypatch):
    share = tmp_path / "share"
    installed = share / "applications" / f"{APP_ID}.desktop"
    installed.parent.mkdir(parents=True)
    installed.write_text("[Desktop Entry]\nX-GNOME-Autostart-enabled=true\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(share))

    autostart.set_enabled(True)
    assert paths.desktop.read_text().count("X-GNOME-Autostart-enabled") == 1


def test_the_launcher_is_also_looked_up_in_the_system_data_dirs(tmp_path, monkeypatch):
    system = tmp_path / "usr-share"
    installed = system / "applications" / f"{APP_ID}.desktop"
    installed.parent.mkdir(parents=True)
    installed.write_text("[Desktop Entry]\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("XDG_DATA_DIRS", f":{system}")  # empty segments skipped
    assert autostart._find_installed_desktop() == installed


def test_no_installed_launcher_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "also-empty"))
    assert autostart._find_installed_desktop() is None


def test_the_lookup_falls_back_to_the_default_dirs(monkeypatch):
    # Unset XDG vars must not crash the lookup; it just finds nothing here.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    autostart._find_installed_desktop()  # must not raise


# ── set_enabled: macOS ──────────────────────────────────────────────────────


def test_enabling_on_macos_writes_a_run_at_load_plist(paths, macos):
    assert autostart.set_enabled(True) is True
    body = paths.plist.read_text()
    assert f"<string>{APP_ID}</string>" in body
    assert "<key>RunAtLoad</key>\n    <true/>" in body
    assert f"<string>{sys.executable}</string>" in body


def test_plist_arguments_are_xml_escaped(paths, macos, monkeypatch):
    monkeypatch.setenv("APPIMAGE", "/Apps/a&b<c>.app")
    monkeypatch.setattr(autostart, "FROZEN", True)
    autostart.set_enabled(True)
    body = paths.plist.read_text()
    assert "<string>/Apps/a&amp;b&lt;c&gt;.app</string>" in body


def test_disabling_on_macos_removes_the_plist(paths, macos):
    autostart.set_enabled(True)
    assert autostart.set_enabled(False) is True
    assert not paths.plist.exists()


# ── set_enabled: Windows ────────────────────────────────────────────────────


def test_enabling_on_windows_writes_the_run_value(windows):
    fake = windows()
    assert autostart.set_enabled(True) is True
    assert APP_NAME in fake.values
    # Quoted so a path with spaces survives the shell the OS runs it through.
    assert "wactorz.desktop.app" in fake.values[APP_NAME]


def test_disabling_on_windows_deletes_the_run_value(windows):
    fake = windows({APP_NAME: "whatever"})
    assert autostart.set_enabled(False) is True
    assert APP_NAME not in fake.values


def test_disabling_on_windows_tolerates_a_missing_value(windows):
    windows()
    assert autostart.set_enabled(False) is True


# ── failure is reported, never raised ───────────────────────────────────────


def test_set_enabled_reports_failure_instead_of_raising(paths, monkeypatch):
    # The tray toggle calls this directly; an exception would take down the menu.
    monkeypatch.setattr(autostart, "_DESKTOP", paths.desktop.parent)  # a directory
    paths.desktop.parent.mkdir(parents=True)
    assert autostart.set_enabled(True) is False
