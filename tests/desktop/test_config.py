"""Shared desktop constants.

Mostly module-level derivations; what is worth pinning is the port choice — the
shell tells the window a URL before the backend exists, so a port that turns out
to be someone else's server means the window attaches to another instance's UI.
"""

# pylint: disable=missing-function-docstring

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from wactorz.desktop import config


@pytest.fixture(name="os_name")
def os_name_fixture(monkeypatch):
    """Pretend to be another OS *inside this module only*.

    `os.name` cannot be patched globally: pathlib dispatches Path() on it, and a
    WindowsPath cannot be instantiated on a posix host.
    """

    def _set(name):
        monkeypatch.setattr(config, "os", SimpleNamespace(name=name, environ=os.environ))

    return _set


# ── port ────────────────────────────────────────────────────────────────────


def test_a_dynamic_port_is_free_and_on_loopback():
    port = config._free_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((config.HOST, port))  # still free: nothing was left listening


def test_an_explicit_monitor_port_is_honoured(monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "8123")
    assert config._resolve_port() == 8123


def test_a_junk_monitor_port_falls_back_to_a_free_one(monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "not-a-port")
    assert config._resolve_port() != 0


def test_an_empty_monitor_port_falls_back_to_a_free_one(monkeypatch):
    monkeypatch.setenv("MONITOR_PORT", "")
    assert config._resolve_port() != 0


def test_no_monitor_port_picks_a_free_one(monkeypatch):
    monkeypatch.delenv("MONITOR_PORT", raising=False)
    assert config._resolve_port() != 0


def test_the_url_is_loopback_only():
    # The backend must never be reachable from the network in desktop mode.
    assert f"http://127.0.0.1:{config.PORT}" == config.URL


# ── data dir ────────────────────────────────────────────────────────────────


def test_the_data_dir_follows_xdg_on_posix(os_name, monkeypatch, tmp_path):
    os_name("posix")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert config._data_dir() == tmp_path / "wactorz"


def test_the_data_dir_falls_back_to_local_share(os_name, monkeypatch):
    os_name("posix")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert config._data_dir() == Path.home() / ".local" / "share" / "wactorz"


def test_the_data_dir_uses_localappdata_on_windows(os_name, monkeypatch, tmp_path):
    os_name("nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config._data_dir() == tmp_path / "wactorz"


def test_the_data_dir_falls_back_to_appdata_local_on_windows(os_name, monkeypatch):
    os_name("nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert config._data_dir() == Path.home() / "AppData" / "Local" / "wactorz"


def test_state_files_live_under_the_data_dir():
    assert config.BACKEND_LOG.parent == config.DATA_DIR
    assert config.WINDOW_STATE_FILE.parent == config.DATA_DIR
