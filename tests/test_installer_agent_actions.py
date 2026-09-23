"""What the installer does with each action it is asked for.

Locally it installs into the interpreter running Wactorz, one package at a
time, so one failure is reported without costing the rest. A name that is not a
package — an option, a URL — is refused before pip sees it, and a package that
already imports is not reinstalled.

On a node it works over SSH with credentials taken from the configured deploy
target, never from the task. `tests/test_deploy_broker_tls.py` and
`tests/test_node_control_signing.py` cover what a deploy writes; this covers the
order it runs in and what it reports when a step cannot happen.
"""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncssh
import pytest

from wactorz import config as config_mod
from wactorz._version import __version__
from wactorz.agents import installer_agent, node_service
from wactorz.agents.installer_agent import InstallerAgent, NodeTls
from wactorz.config import CONFIG, DeployTarget
from wactorz.core.actor import Message, MessageType


class _Conn:
    """An SSH connection answering each command by the first matching fragment."""

    def __init__(self, answers: dict[str, tuple[bool, str]] | None = None) -> None:
        self.answers = answers or {}
        self.commands: list[str] = []
        self.sftp = _Sftp()

    async def run(self, command: str, check: bool = False) -> Any:
        self.commands.append(command)
        ok, output = next(
            (answer for fragment, answer in self.answers.items() if fragment in command),
            (True, ""),
        )
        return SimpleNamespace(exit_status=0 if ok else 1, stdout=output, stderr="")

    def start_sftp_client(self) -> "_Sftp":
        # One object across calls, so a test can read every upload the deploy
        # made rather than only the last client's.
        return self.sftp

    async def __aenter__(self) -> "_Conn":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Sftp:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    async def put(self, local: str, remote: str) -> None:
        self.uploads.append((local, remote))

    async def __aenter__(self) -> "_Sftp":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture(name="installer")
def installer_fixture(tmp_path: Path) -> InstallerAgent:
    return InstallerAgent(persistence_dir=str(tmp_path))


@pytest.fixture(name="conn")
def conn_fixture(installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch) -> _Conn:
    conn = _Conn()

    async def _kwargs(payload: dict[str, Any]) -> dict[str, Any]:
        return {"host": payload.get("host")}

    monkeypatch.setattr(installer, "_ssh_kwargs", _kwargs)
    monkeypatch.setattr(installer_agent.asyncssh, "connect", lambda **_kw: conn)
    monkeypatch.setattr(installer, "_log_remote", lambda _message: None)
    return conn


def _pip(
    installer: InstallerAgent,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: set[str] = frozenset(),  # pyright: ignore[reportArgumentType]
    failing: set[str] = frozenset(),  # pyright: ignore[reportArgumentType]
) -> list[str]:
    ran: list[str] = []

    async def _install(package: str) -> tuple[bool, str]:
        ran.append(package)
        return (package not in failing), (
            "ERROR: no matching distribution" if package in failing else "ok"
        )

    monkeypatch.setattr(installer, "_pip_install", _install)
    monkeypatch.setattr(installer, "_is_installed", lambda name: name in installed)
    return ran


class TestDispatch:
    async def test_every_action_reaches_its_handler(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reached: list[tuple[str, Any]] = []

        async def _async(name: str, payload: Any) -> dict[str, Any]:
            reached.append((name, payload))
            return {}

        monkeypatch.setattr(installer, "_install_packages", lambda p: _async("install", p))
        monkeypatch.setattr(
            installer, "_node_install", lambda p: _async("node_install", p["action"])
        )
        monkeypatch.setattr(installer, "_node_deploy", lambda p: _async("node_deploy", None))
        monkeypatch.setattr(installer, "_node_run", lambda p: _async("node_run", None))

        for payload in (
            {"packages": "numpy, pandas"},
            {"action": "node_install"},
            {"action": "node_install_for_agent"},
            {"action": "node_deploy"},
            {"action": "node_run"},
        ):
            await installer._handle_install(
                Message(type=MessageType.TASK, sender_id="m", payload=payload)
            )

        assert reached == [
            ("install", ["numpy", "pandas"]),
            ("node_install", "node_install"),
            ("node_install", "node_install_for_agent"),
            ("node_deploy", None),
            ("node_run", None),
        ]

    async def test_check_resolve_history_and_unknown(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "_is_installed", lambda name: name == "cv2")
        installer._install_log = [{"package": str(i)} for i in range(25)]

        async def _ask(payload: Any) -> dict[str, Any]:
            return await installer._handle_install(
                Message(type=MessageType.TASK, sender_id="m", payload=payload)
            )

        assert await _ask({"action": "check", "packages": "cv2 yaml"}) == {
            "status": {"cv2": "installed", "yaml": "missing"}
        }
        assert await _ask({"action": "resolve", "imports": ["cv2", "requests"]}) == {
            "resolved": {"cv2": "opencv-python", "requests": "requests"}
        }
        assert len((await _ask({"action": "history"}))["history"]) == 20
        assert await _ask({"action": "uninstall"}) == {"error": "Unknown action: uninstall"}
        assert await _ask("not a dict") == {"error": "No packages specified"}

    async def test_a_task_is_answered_with_its_task_id(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[tuple[str, Any]] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, payload))
            return True

        monkeypatch.setattr(installer, "send", _send)

        await installer.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="catalog",
                reply_to="main",
                payload={"action": "history", "_task_id": "t1"},
            )
        )
        await installer.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="x"))

        assert sent == [("main", {"history": [], "task": "t1", "_task_id": "t1"})]

    async def test_start_announces_the_interpreter(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[str] = []

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            published.append(topic)

        monkeypatch.setattr(installer, "_mqtt_publish", _publish)

        await installer.on_start()

        assert published == [
            f"agents/{installer.actor_id}/logs",
            f"agents/{installer.actor_id}/manifest",
        ]
        assert installer._current_task_description() == "idle"


class TestLocalInstall:
    async def test_nothing_to_install_is_an_error(self, installer: InstallerAgent) -> None:
        assert await installer._install_packages([]) == {"error": "No packages specified"}

    async def test_each_package_is_reported_on_its_own(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran = _pip(installer, monkeypatch, installed={"PIL"}, failing={"broken-pkg"})

        result = await installer._install_packages(
            ["cv2", "pillow", "", "--index-url=http://evil", "broken-pkg"]
        )

        assert ran == ["opencv-python", "broken-pkg"]
        assert result["results"]["opencv-python"] == "installed"
        assert result["results"]["pillow"] == "already_installed"
        assert result["results"]["--index-url=http://evil"] == "refused"
        assert result["results"]["broken-pkg"].startswith("failed: ERROR")
        assert result["failed"] == ["--index-url=http://evil", "broken-pkg"]
        assert result["success"] is False
        assert [entry["package"] for entry in installer._install_log] == [
            "opencv-python",
            "broken-pkg",
        ]

    @pytest.mark.parametrize(
        ("requested", "alternative"),
        [("duckduckgo-search", "ddgs"), ("pdfplumber", "pymupdf")],
    )
    async def test_a_renamed_or_fragile_package_tries_its_alternative(
        self,
        installer: InstallerAgent,
        monkeypatch: pytest.MonkeyPatch,
        requested: str,
        alternative: str,
    ) -> None:
        ran = _pip(installer, monkeypatch, failing={requested})

        result = await installer._install_packages([requested])

        assert ran == [requested, alternative]
        assert result["results"] == {alternative: "installed"}
        assert result["success"] is True

    async def test_pip_runs_with_this_interpreter(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []

        def _run(cmd: list[str], **_kwargs: Any) -> Any:
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"Successfully installed", stderr=b"")

        monkeypatch.setattr(subprocess, "run", _run)

        assert await installer._pip_install("requests") == (True, "Successfully installed")
        assert commands[0][:5] == [
            installer_agent.sys.executable,
            "-m",
            "pip",
            "install",
            "requests",
        ]

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (subprocess.TimeoutExpired("pip", 180), "pip timed out after 180s"),
            (FileNotFoundError("python"), "Python executable not found"),
            (PermissionError("denied"), "PermissionError: denied"),
        ],
    )
    async def test_a_pip_that_cannot_run_is_explained(
        self,
        installer: InstallerAgent,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        message: str,
    ) -> None:
        def _run(cmd: list[str], **_kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(subprocess, "run", _run)

        ok, output = await installer._pip_install("requests")

        assert ok is False
        assert message in output

    async def test_an_executor_failure_is_explained(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Loop:
            def run_in_executor(self, *_args: Any) -> Any:
                raise RuntimeError("shutting down")

        monkeypatch.setattr(installer_agent.asyncio, "get_event_loop", lambda: _Loop())

        assert await installer._pip_install("requests") == (
            False,
            "Executor error: RuntimeError: shutting down",
        )

    def test_importability_is_checked_fresh(self, installer: InstallerAgent) -> None:
        assert installer._is_installed("json") is True
        assert installer._is_installed("definitely_not_a_module_here") is False


class TestNodeInstall:
    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            ({"packages": ["x"]}, "Missing 'host' in payload"),
            ({"host": "rpi"}, "No packages specified"),
            ({"host": "rpi", "packages": "ok, --index-url=x"}, "Not package names: --index-url=x"),
        ],
    )
    async def test_a_bad_request_is_refused_before_connecting(
        self, installer: InstallerAgent, conn: _Conn, payload: dict[str, Any], error: str
    ) -> None:
        assert await installer._node_install(payload) == {"error": error}
        assert conn.commands == []

    async def test_the_nodes_venv_pip_is_used_when_it_exists(
        self, installer: InstallerAgent, conn: _Conn
    ) -> None:
        conn.answers = {"test -f": (True, "yes")}

        result = await installer._node_install({"host": "rpi", "packages": ["numpy"]})

        assert result["success"] is True
        assert conn.commands[-1] == "~/wactorz/venv/bin/pip install numpy -q 2>&1"

    async def test_a_missing_venv_is_created_first(
        self, installer: InstallerAgent, conn: _Conn
    ) -> None:
        checks = iter(["no", "yes"])

        async def _run(command: str, check: bool = False) -> Any:
            conn.commands.append(command)
            output = next(checks) if command.startswith("test -f") else ""
            return SimpleNamespace(exit_status=0, stdout=output, stderr="")

        conn.run = _run  # type: ignore[method-assign]

        await installer._node_install({"host": "rpi", "packages": ["numpy"]})

        assert "python3 -m venv ~/wactorz/venv" in conn.commands[1]
        assert conn.commands[-1].startswith("~/wactorz/venv/bin/pip install numpy")

    async def test_without_a_venv_it_falls_back_to_system_pip_and_reports_failure(
        self, installer: InstallerAgent, conn: _Conn
    ) -> None:
        conn.answers = {"test -f": (True, "no"), "python3 -m pip": (False, "externally managed")}

        result = await installer._node_install({"host": "rpi", "packages": ["numpy"]})

        assert "--break-system-packages" in conn.commands[-1]
        assert result == {"success": False, "host": "rpi", "error": "externally managed"}

    async def test_a_connection_that_fails_is_reported(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_target(payload: dict[str, Any]) -> dict[str, Any]:
            raise PermissionError("No deploy target configured for 'rpi'")

        monkeypatch.setattr(installer, "_ssh_kwargs", _no_target)
        monkeypatch.setattr(installer, "_log_remote", lambda _m: None)

        result = await installer._node_install({"host": "rpi", "packages": ["numpy"]})
        ran = await installer._node_run({"host": "rpi", "command": "uptime"})

        assert result["success"] is False and "No deploy target" in result["error"]
        assert ran["success"] is False and "No deploy target" in ran["error"]


class TestNodeRun:
    async def test_a_host_is_required(self, installer: InstallerAgent, conn: _Conn) -> None:
        assert await installer._node_run({"command": "uptime"}) == {
            "error": "Missing 'host' in payload"
        }

    async def test_the_command_output_and_status_are_returned(
        self, installer: InstallerAgent, conn: _Conn
    ) -> None:
        conn.answers = {"uptime": (False, "command not found")}

        result = await installer._node_run({"host": "rpi", "command": "uptime"})

        assert result == {
            "success": False,
            "host": "rpi",
            "command": "uptime",
            "output": "command not found",
            "exit_code": 1,
        }


class TestNodeDeploy:
    @staticmethod
    def _target(monkeypatch: pytest.MonkeyPatch, installer: InstallerAgent) -> None:
        target = installer_agent.DeployTarget(name="rpi", host="10.0.0.5", user="pi", password="x")
        monkeypatch.setattr(installer, "_resolve_ssh_target", lambda _p: target)

    async def test_a_name_that_cannot_be_a_topic_is_refused(
        self, installer: InstallerAgent, conn: _Conn
    ) -> None:
        result = await installer._node_deploy({"host": "10.0.0.5", "node_name": "bad/name"})

        assert result["success"] is False
        assert conn.commands == []

    async def test_an_unconfigured_node_is_refused(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer, "_resolve_ssh_target", lambda _p: None)

        result = await installer._node_deploy({"host": "10.0.0.5", "node_name": "rpi"})

        assert "No deploy target configured for 'rpi'" in result["error"]

    async def test_a_payload_naming_another_host_is_refused(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._target(monkeypatch, installer)

        result = await installer._node_deploy({"host": "10.9.9.9", "node_name": "rpi"})

        assert "Refusing to connect" in result["error"]
        assert conn.commands == []

    async def test_a_full_deploy_runs_every_step_and_reports_supervision(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._target(monkeypatch, installer)
        conn.answers = {
            "cd ~ && pwd": (True, "/root"),
            "pip install aiomqtt": (False, "slow mirror"),
        }
        persisted: list[tuple[str, str, str]] = []

        async def _tls(*_args: Any) -> NodeTls:
            return NodeTls(enabled=True, port=8883, note="checked")

        async def _env(*_args: Any) -> bool:
            return True

        async def _install(run: Any, *, user: str, home: str) -> Any:
            await run("systemctl --version")
            return node_service.NOHUP

        async def _account(*_args: Any) -> None:
            return None

        monkeypatch.setattr(installer, "_decide_node_tls", _tls)
        monkeypatch.setattr(installer, "_check_node_account", _account)
        monkeypatch.setattr(installer, "_put_node_env", _env)
        monkeypatch.setattr(installer_agent.node_service, "install", _install)
        monkeypatch.setattr(
            installer,
            "_persist_node_info",
            lambda **kw: persisted.append((kw["node_name"], kw["host"], kw["user"])),
        )

        result = await installer._node_deploy(
            {"host": "10.0.0.5", "node_name": "rpi", "broker": "10.0.0.1"}
        )

        assert result["success"] is True
        assert result["broker_port"] == 8883
        assert result["tls"] is True
        assert result["supervision"] == node_service.NOHUP.label
        assert conn.commands[0] == "mkdir -p ~/wactorz"
        assert any(c.startswith("pkill -f") for c in conn.commands)
        assert "nohup ~/wactorz/venv/bin/wactorz" in conn.commands[-1]
        assert persisted == [("rpi", "10.0.0.5", "pi")]

    async def test_a_node_that_never_heartbeats_fails_the_deploy_with_its_log(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Started is not connected. A node whose service is up but never reaches
        # the broker is reported as a failure, with what it logged, rather than
        # as a success the dashboard then contradicts.
        self._target(monkeypatch, installer)
        conn.answers = {
            "cd ~ && pwd": (True, "/home/pi"),
            "journalctl": (True, "Publisher error: Not authorized"),
        }
        persisted: list[tuple[str, str, str]] = []

        async def _tls(*_args: Any) -> NodeTls:
            return NodeTls(enabled=False, port=1883, note="plain")

        async def _env(*_args: Any) -> bool:
            return True

        async def _install(run: Any, *, user: str, home: str) -> Any:
            return node_service.USER

        async def _account(*_args: Any) -> None:
            return None

        async def _no_heartbeat(_node_name: str) -> str | None:
            return "The node started but sent no heartbeat within 45s."

        monkeypatch.setattr(installer, "_decide_node_tls", _tls)
        monkeypatch.setattr(installer, "_check_node_account", _account)
        monkeypatch.setattr(installer, "_put_node_env", _env)
        monkeypatch.setattr(installer, "_await_first_heartbeat", _no_heartbeat)
        monkeypatch.setattr(installer_agent.node_service, "install", _install)
        monkeypatch.setattr(
            installer,
            "_persist_node_info",
            lambda **kw: persisted.append((kw["node_name"], kw["host"], kw["user"])),
        )

        result = await installer._node_deploy(
            {"host": "10.0.0.5", "node_name": "rpi", "broker": "10.0.0.1"}
        )

        assert result["success"] is False
        assert result["supervision"] == node_service.USER.label
        assert "no heartbeat" in result["error"]
        assert "Not authorized" in result["error"]
        # The node is not recorded as deployed: nothing says it is reachable.
        assert persisted == []

    async def test_a_failure_part_way_is_reported_with_the_node(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._target(monkeypatch, installer)

        async def _tls(*_args: Any) -> NodeTls:
            raise OSError("sftp closed")

        monkeypatch.setattr(installer, "_decide_node_tls", _tls)

        result = await installer._node_deploy({"host": "10.0.0.5", "node_name": "rpi"})

        assert result == {
            "success": False,
            "node_name": "rpi",
            "host": "10.0.0.5",
            "error": "sftp closed",
        }

    async def test_a_node_that_cannot_install_wactorz_is_reported_as_failed(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The node runs the package. Carrying on to start a unit that has
        # nothing to run would report a successful deploy for a node that never
        # appears, with the reason only in this node's own log.
        self._target(monkeypatch, installer)

        async def _tls(*_args: Any) -> NodeTls:
            return NodeTls(enabled=False, port=1883, note="")

        async def _env(*_args: Any) -> bool:
            return False

        async def _no_install(*_args: Any, **_kw: Any) -> bool:
            return False

        monkeypatch.setattr(installer, "_decide_node_tls", _tls)
        monkeypatch.setattr(installer, "_put_node_env", _env)
        monkeypatch.setattr(installer, "_install_wactorz", _no_install)

        result = await installer._node_deploy({"host": "10.0.0.5", "node_name": "rpi"})

        assert result["success"] is False
        assert "wactorz" in result["error"]
        assert not any("pkill" in c for c in conn.commands), "the node was left half-deployed"


class TestInstallingWactorzOnTheNode:
    """A node runs the package, so the deploy's job is to put it there.

    A checkout deploys itself: running from a source tree means the code that
    matters is here. Anything else installs from PyPI at the version this
    machine runs, and checks that what arrived can actually be a node.
    """

    WHEEL = Path("/tmp/dist/wactorz-9.9.9-py3-none-any.whl")

    def _from_checkout(self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _built() -> Path:
            return self.WHEEL

        monkeypatch.setattr(installer, "_build_wheel", _built)

    def _from_an_installed_package(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_wheel() -> None:
            return None

        monkeypatch.setattr(installer, "_build_wheel", _no_wheel)

    async def test_a_checkout_deploys_its_own_code(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PyPI is not consulted at all.

        At the same version number pip finds the node already satisfied and
        changes nothing, so a deploy of edited code shipped the previous one —
        silently, and exactly when someone is iterating.
        """
        self._from_checkout(installer, monkeypatch)

        assert await installer._install_wactorz(conn, "rpi", "/home/pi") is True

        assert conn.sftp.uploads == [(str(self.WHEEL), f"/home/pi/wactorz/{self.WHEEL.name}")]
        assert not any(f"wactorz=={__version__}" in c for c in conn.commands)

    async def test_the_uploaded_wheel_replaces_a_same_numbered_install(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pip refuses a wheel whose version is already installed.

        "already installed with the same version as the provided wheel" — and
        it leaves the old code in place, which is exactly the code this path
        exists to replace.
        """
        self._from_checkout(installer, monkeypatch)

        await installer._install_wactorz(conn, "rpi", "/home/pi")

        forced = [c for c in conn.commands if "--force-reinstall" in c]
        assert forced, "the wheel was offered to pip without forcing it"
        # Without deps: they came with the call before it, and refetching two
        # dozen packages over a node's link is minutes rather than seconds.
        assert all("--no-deps" in c for c in forced)

    async def test_paths_are_absolute_under_the_home_the_node_reported(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never `~`.

        SFTP does no tilde expansion, so an upload to `~/wactorz/…` creates a
        directory literally named `~`; and the install command quotes its
        argument, which stops a shell expanding one either. Deploying as root,
        whose home is `/root`, is the case that makes this visible.
        """
        self._from_checkout(installer, monkeypatch)

        assert await installer._install_wactorz(conn, "rpi", "/root") is True

        assert conn.sftp.uploads == [(str(self.WHEEL), f"/root/wactorz/{self.WHEEL.name}")]
        assert not any("~" in command for command in conn.commands)

    async def test_an_installed_package_deploys_the_published_one(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._from_an_installed_package(installer, monkeypatch)

        assert await installer._install_wactorz(conn, "rpi", "/home/pi") is True

        (install,) = [c for c in conn.commands if "pip" in c and "install" in c]
        assert f"wactorz=={__version__}" in install
        assert conn.sftp.uploads == []

    async def test_it_checks_the_install_can_actually_be_a_node(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The version is a claim; this is the thing being relied on.
        self._from_an_installed_package(installer, monkeypatch)

        await installer._install_wactorz(conn, "rpi", "/home/pi")

        assert any("import wactorz.node" in c for c in conn.commands)

    async def test_a_published_version_that_cannot_be_a_node_fails_the_deploy(
        self, installer: InstallerAgent, conn: _Conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The number can match while the contents do not.

        Every release before the node runtime existed answers to its own
        version and carries no `wactorz.node`. Starting a unit against one
        would be worse than failing: the runner is started with `--node`, an
        older CLI discards flags it does not know, and the node would quietly
        come up as a second *server*. With no source tree to build from, there
        is nothing else to try, so the deploy says so.
        """
        self._from_an_installed_package(installer, monkeypatch)
        conn.answers = {"import wactorz.node": (False, "ModuleNotFoundError")}

        assert await installer._install_wactorz(conn, "rpi", "/home/pi") is False


def _yes_then_no(installer: InstallerAgent) -> Any:
    """Refuse the first install and accept the second, as an upgrade goes."""
    seen: list[int] = []

    async def _can_run(_conn: Any, _home: str) -> bool:
        seen.append(1)
        return len(seen) > 1

    return _can_run


class TestRemoteLog:
    async def test_a_remote_step_is_logged_and_published(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[str] = []

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            published.append(payload["message"])

        monkeypatch.setattr(installer, "_mqtt_publish", _publish)

        installer._log_remote("step one")
        await installer_agent.asyncio.sleep(0)

        assert published == ["step one"]


class _HostKey:
    """A server host key as `get_server_host_key` returns it."""

    def __init__(self) -> None:
        self._key = asyncssh.generate_private_key("ssh-ed25519")

    def export_public_key(self, fmt: str) -> bytes:
        return self._key.export_public_key(fmt)

    def get_fingerprint(self) -> str:
        return self._key.get_fingerprint()


def _deploy_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "deploy_known_hosts": str(tmp_path / "ssh" / "known_hosts"),
        "deploy_strict_host_keys": False,
        "deploy_targets": (),
    }
    values.update(overrides)
    monkeypatch.setattr(installer_agent, "CONFIG", replace(CONFIG, **values))
    monkeypatch.setattr(config_mod, "CONFIG", replace(config_mod.CONFIG, **values))


class TestHostKeyTrust:
    async def test_a_new_host_is_learned_once_and_recorded_privately(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path)
        key = _HostKey()
        fetched: list[tuple[str, int]] = []

        async def _fetch(host: str, port: int) -> _HostKey:
            fetched.append((host, port))
            return key

        monkeypatch.setattr(installer_agent.asyncssh, "get_server_host_key", _fetch)
        monkeypatch.setattr(installer, "_log_remote", lambda _m: None)

        first = await installer._known_hosts("10.0.0.5", 2222)
        second = await installer._known_hosts("10.0.0.5", 2222)

        path = tmp_path / "ssh" / "known_hosts"
        assert first == second == str(path)
        assert fetched == [("10.0.0.5", 2222)], "a known host is never re-learned"
        assert path.read_text(encoding="utf-8").startswith("[10.0.0.5]:2222 ssh-ed25519 ")
        if sys.platform != "win32":  # Windows has no owner-only mode bits to check
            assert path.stat().st_mode & 0o777 == 0o600

    async def test_strict_mode_refuses_an_unknown_host(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path, deploy_strict_host_keys=True)

        with pytest.raises(PermissionError, match="DEPLOY_STRICT_HOST_KEYS is set"):
            await installer._known_hosts("10.0.0.5", 22)

    async def test_a_host_offering_no_key_is_refused(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path)

        async def _none(host: str, port: int) -> None:
            return None

        monkeypatch.setattr(installer_agent.asyncssh, "get_server_host_key", _none)

        with pytest.raises(PermissionError, match="offered no SSH host key"):
            await installer._known_hosts("10.0.0.5", 22)

    def test_the_file_follows_the_state_directory_unless_pinned(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path, deploy_known_hosts="")
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "state"))

        assert installer._known_hosts_path() == tmp_path / "state" / "known_hosts"


class TestSshCredentials:
    TARGET = DeployTarget(
        name="rpi", host="10.0.0.5", user="pi", key_path="/keys/id", password="pw"
    )

    async def test_credentials_come_only_from_the_configured_target(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path, deploy_targets=(self.TARGET,))

        async def _trusted(host: str, port: int) -> str:
            return "/known"

        monkeypatch.setattr(installer, "_known_hosts", _trusted)

        kwargs = await installer._ssh_kwargs({"host": "10.0.0.5", "password": "from-chat"})

        assert kwargs == {
            "host": "10.0.0.5",
            "port": 22,
            "username": "pi",
            "known_hosts": "/known",
            "client_keys": ["/keys/id"],
            "password": "pw",
        }

    async def test_a_target_is_found_by_node_name_too(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _deploy_config(monkeypatch, tmp_path, deploy_targets=(self.TARGET,))

        assert installer._resolve_ssh_target({"node_name": "rpi"}) == self.TARGET
        assert installer._resolve_ssh_target({"host": "10.0.0.5"}) == self.TARGET
        assert installer._resolve_ssh_target({"host": "10.9.9.9"}) is None

    async def test_an_unconfigured_host_or_a_target_without_credentials_is_refused(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bare = DeployTarget(name="bare", host="10.0.0.6", user="pi")
        _deploy_config(monkeypatch, tmp_path, deploy_targets=(bare,))

        with pytest.raises(PermissionError, match="No deploy target configured"):
            await installer._ssh_kwargs({"host": "10.9.9.9"})
        with pytest.raises(PermissionError, match="has no credentials"):
            await installer._ssh_kwargs({"host": "10.0.0.6"})

    async def test_a_target_without_a_host_is_found_by_mdns(
        self, installer: InstallerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        looked_up: list[str] = []

        def _resolve(name: str) -> str:
            looked_up.append(name)
            if name == "missing.local":
                raise OSError("unknown host")
            return "10.0.0.42"

        monkeypatch.setattr(installer_agent.socket, "gethostbyname", _resolve)

        found = await installer._resolve_target_host(DeployTarget(name="rpi", host=""), "")

        assert found == "10.0.0.42"
        assert looked_up == ["rpi.local"]
        with pytest.raises(PermissionError, match="does not resolve"):
            await installer._resolve_target_host(DeployTarget(name="missing", host=""), "")


class TestRememberedNodes:
    def test_where_a_node_lives_is_recorded_without_a_secret(
        self, installer: InstallerAgent
    ) -> None:
        installer._persist_node_info(node_name="rpi", host="10.0.0.5", user="pi")

        assert installer.recall("_node_credentials") == {"rpi": {"host": "10.0.0.5", "user": "pi"}}
        assert installer.recall("node_host_rpi") == "10.0.0.5"

    def test_passwords_stored_by_older_versions_are_removed(
        self, installer: InstallerAgent
    ) -> None:
        installer.persist(
            "_node_credentials",
            {"rpi": {"host": "10.0.0.5", "user": "pi", "password": "old"}, "junk": "x"},
        )

        installer._scrub_persisted_credentials()

        assert installer.recall("_node_credentials") == {"rpi": {"host": "10.0.0.5", "user": "pi"}}
