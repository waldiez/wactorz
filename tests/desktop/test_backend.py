"""The backend child process and the probes that gate starting it.

Nothing here spawns a real process or opens a real socket: `subprocess.Popen`,
`socket.create_connection` and `urllib.request.urlopen` are patched on the
module under test, and `time.sleep` is neutralised so the polling loops run at
full speed.

The behaviour worth pinning is what the shell depends on: a child that dies
early is reported as not-serving (rather than waited out), a broker that appears
late is still picked up, and stop() escalates to kill when terminate is ignored.
"""

# pylint: disable=missing-function-docstring

import subprocess
import sys
from types import SimpleNamespace

import pytest

from wactorz.desktop import backend


class _NullCtx:
    """Minimal context manager for the patched urlopen / create_connection."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeProc:
    """A child process whose exit can be scripted."""

    def __init__(self, exit_code=None):
        self._exit_code = exit_code  # None = still running
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._exit_code = 0

    def kill(self):
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout=None):
        self.waited = True
        return self._exit_code


@pytest.fixture(name="no_process", autouse=True)
def no_process_fixture(monkeypatch):
    """Start every test with no child, and never really sleep."""
    monkeypatch.setattr(backend, "_process", None)
    monkeypatch.setattr(backend.time, "sleep", lambda _s: None)
    yield
    backend._process = None


@pytest.fixture(name="popen_calls")
def popen_calls_fixture(monkeypatch):
    """Capture Popen invocations instead of spawning anything."""
    calls: list = []

    def _popen(cmd, **kwargs):
        calls.append(SimpleNamespace(cmd=cmd, kwargs=kwargs))
        return _FakeProc()

    monkeypatch.setattr(backend.subprocess, "Popen", _popen)
    monkeypatch.setattr(backend.backend_config, "env_for_backend", dict)
    return calls


# ── is_running / stop ───────────────────────────────────────────────────────


def test_is_running_is_false_without_a_child():
    assert backend.is_running() is False


def test_is_running_tracks_the_child_state(monkeypatch):
    monkeypatch.setattr(backend, "_process", _FakeProc())
    assert backend.is_running() is True
    monkeypatch.setattr(backend, "_process", _FakeProc(exit_code=1))
    assert backend.is_running() is False


def test_stop_terminates_a_live_child(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(backend, "_process", proc)
    backend.stop()
    assert proc.terminated and not proc.killed
    assert backend.is_running() is False


def test_stop_escalates_to_kill_when_terminate_is_ignored(monkeypatch):
    proc = _FakeProc()

    def _stubborn(timeout=None):
        raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 5)

    proc.wait = _stubborn
    monkeypatch.setattr(backend, "_process", proc)
    backend.stop()
    assert proc.killed


def test_stop_is_safe_with_no_child():
    backend.stop()  # must not raise
    assert backend.is_running() is False


def test_stop_clears_an_already_exited_child(monkeypatch):
    proc = _FakeProc(exit_code=0)
    monkeypatch.setattr(backend, "_process", proc)
    backend.stop()
    assert not proc.terminated  # nothing to signal
    assert backend._process is None


# ── start ───────────────────────────────────────────────────────────────────


def test_start_spawns_with_the_shell_owned_env(popen_calls):
    backend.start()
    env = popen_calls[0].kwargs["env"]
    # INTERFACE=rest is what makes the backend bind a web server at all.
    assert env["INTERFACE"] == "rest"
    assert env["MONITOR_PORT"] == str(backend.PORT)
    assert env["WACTORZ_STATE_DIR"].endswith("state")
    assert backend.is_running() is True


def test_start_lets_user_config_override_the_inherited_env(popen_calls, monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "from-shell")
    monkeypatch.setattr(backend.backend_config, "env_for_backend", lambda: {"MQTT_HOST": "from-ui"})
    backend.start()
    assert popen_calls[0].kwargs["env"]["MQTT_HOST"] == "from-ui"


def test_start_keeps_shell_owned_vars_above_user_config(popen_calls, monkeypatch):
    # A user cannot redirect the port the shell already told the window about.
    monkeypatch.setattr(backend.backend_config, "env_for_backend", lambda: {"MONITOR_PORT": "9999"})
    backend.start()
    assert popen_calls[0].kwargs["env"]["MONITOR_PORT"] == str(backend.PORT)


def test_start_still_runs_when_the_log_file_cannot_be_opened(monkeypatch):
    # Losing captured output must not stop the backend from running.
    calls: list = []
    monkeypatch.setattr(backend.backend_config, "env_for_backend", dict)
    monkeypatch.setattr(
        backend, "open", lambda *a, **k: (_ for _ in ()).throw(OSError), raising=False
    )

    def _popen(cmd, **kwargs):
        calls.append(kwargs)
        return _FakeProc()

    monkeypatch.setattr(backend.subprocess, "Popen", _popen)
    backend.start()
    assert len(calls) == 1
    assert "stdout" not in calls[0]  # fell back to the un-redirected form


# ── wait_until_serving ──────────────────────────────────────────────────────


def _urlopen_ok(*_a, **_k):
    return _NullCtx()


def test_wait_until_serving_returns_true_once_health_answers(monkeypatch):
    monkeypatch.setattr(backend.urllib.request, "urlopen", _urlopen_ok)
    assert backend.wait_until_serving(timeout=1) is True


def test_wait_until_serving_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(
        backend.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("refused")),
    )
    ticks = iter([0.0, 0.5, 99.0])  # start, one poll, then past the deadline
    monkeypatch.setattr(backend.time, "time", lambda: next(ticks))
    assert backend.wait_until_serving(timeout=1) is False


def test_wait_until_serving_bails_out_when_the_child_dies(monkeypatch):
    # No point waiting out 30s for a process that already exited.
    monkeypatch.setattr(backend, "_process", _FakeProc(exit_code=1))
    monkeypatch.setattr(backend.urllib.request, "urlopen", _urlopen_ok)
    assert backend.wait_until_serving(timeout=30) is False


# ── mqtt probes ─────────────────────────────────────────────────────────────


@pytest.fixture(name="connections")
def connections_fixture(monkeypatch):
    """Record connection attempts; refuse by default."""
    attempts: list = []

    def _connect(address, timeout=None):
        attempts.append(address)
        raise OSError("refused")

    monkeypatch.setattr(backend.socket, "create_connection", _connect)
    monkeypatch.setattr(backend.backend_config, "env_for_backend", dict)
    return attempts


def test_mqtt_reachable_is_false_when_refused(connections):
    assert backend.mqtt_reachable() is False


def test_mqtt_reachable_is_true_when_the_socket_opens(monkeypatch):
    monkeypatch.setattr(backend.backend_config, "env_for_backend", dict)
    monkeypatch.setattr(
        backend.socket,
        "create_connection",
        lambda *a, **k: _NullCtx(),
    )
    assert backend.mqtt_reachable() is True


def test_mqtt_probe_defaults_to_localhost_1883(connections, monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.delenv("MQTT_PORT", raising=False)
    backend.mqtt_reachable()
    assert connections == [("localhost", 1883)]


def test_mqtt_probe_prefers_user_config_over_the_environment(connections, monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "from-shell")
    monkeypatch.setattr(
        backend.backend_config,
        "env_for_backend",
        lambda: {"MQTT_HOST": "broker.local", "MQTT_PORT": "8883"},
    )
    backend.mqtt_reachable()
    assert connections == [("broker.local", 8883)]


def test_mqtt_probe_falls_back_to_1883_on_a_junk_port(connections, monkeypatch):
    monkeypatch.setattr(
        backend.backend_config, "env_for_backend", lambda: {"MQTT_PORT": "eighteen"}
    )
    backend.mqtt_reachable()
    assert connections == [("localhost", 1883)]


def test_wait_for_mqtt_returns_as_soon_as_the_broker_answers(monkeypatch):
    monkeypatch.setattr(backend, "mqtt_reachable", lambda timeout=0: True)
    assert backend.wait_for_mqtt(timeout=5) is True


def test_wait_for_mqtt_picks_up_a_broker_that_starts_late(monkeypatch):
    # The common case at login: the broker container is still booting.
    results = iter([False, False, True])
    monkeypatch.setattr(backend, "mqtt_reachable", lambda timeout=0: next(results))
    assert backend.wait_for_mqtt(timeout=30) is True


def test_wait_for_mqtt_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(backend, "mqtt_reachable", lambda timeout=0: False)
    ticks = iter([0.0, 99.0])  # deadline computed, then already past it
    monkeypatch.setattr(backend.time, "time", lambda: next(ticks))
    assert backend.wait_for_mqtt(timeout=1) is False


# ── run_in_process ──────────────────────────────────────────────────────────


def test_run_in_process_runs_the_package_as_main(monkeypatch):
    import runpy

    seen: list = []
    monkeypatch.setattr(runpy, "run_module", lambda mod, run_name: seen.append((mod, run_name)))
    monkeypatch.setattr(sys, "argv", ["wactorz-desktop", "--run-backend"])
    backend.run_in_process()
    assert seen == [("wactorz", "__main__")]
    # argv is rewritten so the backend does not re-parse the shell's flags.
    assert sys.argv == ["wactorz"]
