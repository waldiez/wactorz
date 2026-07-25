"""Linux .desktop + icon installation.

The installed launcher is not cosmetic: autostart.py copies it, the portal
resolves the app id from it, and StartupWMClass is what binds the running window
to its taskbar icon. All paths are redirected at tmp dirs.
"""

# pylint: disable=missing-function-docstring

import sys

import pytest

from wactorz.desktop import desktop_entry
from wactorz.desktop.config import APP_ID, APP_NAME


@pytest.fixture(name="paths", autouse=True)
def paths_fixture(tmp_path, monkeypatch):
    """Redirect the launcher, icon and bundled-asset paths."""
    apps = tmp_path / "applications"
    icon_dir = tmp_path / "icons"
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(desktop_entry, "_APPS", apps)
    monkeypatch.setattr(desktop_entry, "_ICON_DIR", icon_dir)
    monkeypatch.setattr(desktop_entry, "_DESKTOP", apps / f"{APP_ID}.desktop")
    monkeypatch.setattr(desktop_entry, "_ICON", icon_dir / f"{APP_ID}.png")
    monkeypatch.setattr(desktop_entry, "_ASSETS", assets)
    monkeypatch.setattr(desktop_entry, "FROZEN", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("APPIMAGE", raising=False)
    return desktop_entry


# ── the Exec line ───────────────────────────────────────────────────────────


def test_exec_runs_the_module_from_source():
    assert desktop_entry._exec() == f"{sys.executable} -m wactorz.desktop.app"


def test_exec_uses_the_frozen_executable(monkeypatch):
    monkeypatch.setattr(desktop_entry, "FROZEN", True)
    assert desktop_entry._exec() == sys.executable


def test_exec_prefers_the_appimage_path(monkeypatch):
    monkeypatch.setenv("APPIMAGE", "/home/u/Apps/Wactorz.AppImage")
    monkeypatch.setattr(desktop_entry, "FROZEN", True)
    assert desktop_entry._exec() == "/home/u/Apps/Wactorz.AppImage"


# ── install ─────────────────────────────────────────────────────────────────


def test_install_writes_a_launcher():
    desktop_entry.install()
    body = desktop_entry._DESKTOP.read_text()
    assert body.startswith("[Desktop Entry]\n")
    assert f"Name={APP_NAME}\n" in body
    assert f"Exec={desktop_entry._exec()}\n" in body
    # What binds the running window to this launcher in the taskbar.
    assert f"StartupWMClass={APP_ID}\n" in body


def test_install_copies_the_bundled_icon_and_names_it_by_theme():
    # The bundled icon lives at a transient AppImage mount path.
    (desktop_entry._ASSETS / "icon.png").write_bytes(b"\x89PNG-data")
    desktop_entry.install()
    assert desktop_entry._ICON.read_bytes() == b"\x89PNG-data"
    assert f"Icon={APP_ID}\n" in desktop_entry._DESKTOP.read_text()


def test_a_missing_bundled_icon_leaves_the_icon_key_empty():
    desktop_entry.install()  # no icon.png in assets
    assert not desktop_entry._ICON.exists()
    assert "Icon=\n" in desktop_entry._DESKTOP.read_text()


def test_install_is_idempotent():
    desktop_entry.install()
    first = desktop_entry._DESKTOP.read_text()
    desktop_entry.install()
    assert desktop_entry._DESKTOP.read_text() == first


def test_install_rewrites_a_stale_exec_line(monkeypatch):
    # Rewritten each launch so an AppImage moved by the user keeps working.
    desktop_entry.install()
    monkeypatch.setenv("APPIMAGE", "/new/place/Wactorz.AppImage")
    desktop_entry.install()
    assert "Exec=/new/place/Wactorz.AppImage\n" in desktop_entry._DESKTOP.read_text()


# ── other platforms / failure ───────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_install_is_a_no_op_off_linux(platform, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    desktop_entry.install()
    assert not desktop_entry._DESKTOP.exists()


def test_install_never_raises_when_the_paths_are_unwritable(monkeypatch, tmp_path):
    # Best-effort: losing the menu entry must not stop the app from starting.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setattr(desktop_entry, "_DESKTOP", blocked)
    desktop_entry.install()  # must not raise
