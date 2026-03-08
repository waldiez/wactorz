"""
InstallerAgent — pre-defined agent that installs Python packages on demand.
Always uses sys.executable so packages land in the active venv (e.g. myenv),
not the system Python.
"""

import asyncio
import importlib
import logging
import sys
import time

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)


# pip package name → importable module name
PACKAGE_TO_IMPORT = {
    "opencv-python":     "cv2",
    "pillow":            "PIL",
    "scikit-learn":      "sklearn",
    "beautifulsoup4":    "bs4",
    "pymupdf":           "fitz",
    "python-docx":       "docx",
    "pdfplumber":        "pdfplumber",
    "pymupdf":           "fitz",
    "httpx":             "httpx",
    "requests":          "requests",
    "numpy":             "numpy",
    "pandas":            "pandas",
    "torch":             "torch",
    "transformers":      "transformers",
    "ultralytics":       "ultralytics",
    "pyserial":          "serial",
    "duckduckgo-search": "duckduckgo_search",
    "ddgs":              "duckduckgo_search",
    "asyncssh":          "asyncssh",
    "rich":              "rich",
    "tqdm":              "tqdm",
    "lxml":              "lxml",
    "aiohttp":           "aiohttp",
}

# importable module name → pip package name (for when user gives import names)
IMPORT_TO_PACKAGE = {
    "cv2":               "opencv-python",
    "PIL":               "pillow",
    "sklearn":           "scikit-learn",
    "bs4":               "beautifulsoup4",
    "fitz":              "pymupdf",
    "docx":              "python-docx",
    "pdfplumber":        "pdfplumber",
    "pymupdf":           "fitz",
    "httpx":             "httpx",
    "requests":          "requests",
    "numpy":             "numpy",
    "pandas":            "pandas",
    "torch":             "torch",
    "transformers":      "transformers",
    "ultralytics":       "ultralytics",
    "serial":            "pyserial",
    "duckduckgo_search": "duckduckgo-search",
    "ddgs":              "duckduckgo-search",
    "asyncssh":          "asyncssh",
    "rich":              "rich",
    "tqdm":              "tqdm",
    "lxml":              "lxml",
    "aiohttp":           "aiohttp",
}


class InstallerAgent(Actor):
    """
    Pre-defined agent that installs Python packages on demand.
    Uses sys.executable so packages are installed into the active venv.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "installer")
        super().__init__(**kwargs)
        self.protected    = True
        self._install_log: list[dict] = []

    def _current_task_description(self) -> str:
        return "idle"

    async def on_start(self):
        logger.info(f"[{self.name}] Installer ready — using: {sys.executable}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Installer ready ({sys.executable})", "timestamp": time.time()},
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
        action  = payload.get("action", "install")

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
            # payload: {host, user, packages, password (opt), key_path (opt)}
            return await self._node_install(payload)

        if action == "node_deploy":
            # Full bootstrap: copy remote_runner.py + install deps + start runner
            # payload: {host, user, node_name, broker, password (opt), key_path (opt)}
            return await self._node_deploy(payload)

        if action == "node_run":
            # Run an arbitrary command on a remote node via SSH
            # payload: {host, user, command, password (opt), key_path (opt)}
            return await self._node_run(payload)

        return {"error": f"Unknown action: {action}"}

    # ── Core install logic ──────────────────────────────────────────────────

    async def _install_packages(self, packages: list[str]) -> dict:
        if not packages:
            return {"error": "No packages specified"}

        results = {}
        failed  = []

        for pkg in packages:
            pkg = pkg.strip()
            if not pkg:
                continue

            # Resolve import name → pip name (e.g. "cv2" → "opencv-python")
            pip_name = IMPORT_TO_PACKAGE.get(pkg, pkg)

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

            self._install_log.append({
                "package":   pip_name,
                "success":   success,
                "timestamp": time.time(),
                "output":    output[-500:],
            })

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
            "failed":  failed,
            "success": len(failed) == 0,
            "message": f"Installed {len(results) - len(failed)}/{len(results)} packages",
        }

    async def _pip_install(self, package: str) -> tuple[bool, str]:
        """Run pip install using the same interpreter that launched this process.

        sys.executable inside a venv points to  venv/Scripts/python.exe  (Windows)
        or  venv/bin/python  (Linux/Mac), so packages always land in the right place.
        --break-system-packages is Linux-only and skipped on Windows.
        """
        cmd = [sys.executable, "-m", "pip", "install", package]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            output = (stdout + stderr).decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return False, output

            # Refresh import machinery so the new package is visible immediately
            importlib.invalidate_caches()
            return True, output

        except asyncio.TimeoutError:
            return False, "Timed out after 180s"
        except Exception as e:
            return False, str(e)

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
            pip_name    = IMPORT_TO_PACKAGE.get(pkg, pkg)
            import_name = PACKAGE_TO_IMPORT.get(pip_name, pip_name)
            status[pkg] = "installed" if self._is_installed(import_name) else "missing"
        return {"status": status}

    def _resolve_imports(self, imports: list[str]) -> dict:
        return {"resolved": {imp: IMPORT_TO_PACKAGE.get(imp, imp) for imp in imports}}

    # ── Remote node helpers (SSH via asyncssh) ──────────────────────────────

    def _ssh_kwargs(self, payload: dict) -> dict:
        """Build asyncssh connection kwargs from a task payload."""
        kwargs = dict(
            host        = payload["host"],
            username    = payload.get("user", "pi"),
            known_hosts = None,   # disable host key checking for LAN deploys
        )
        if payload.get("password"):
            kwargs["password"] = payload["password"]
        if payload.get("key_path"):
            kwargs["client_keys"] = [payload["key_path"]]
        return kwargs

    async def _ssh_run(self, conn, command: str) -> tuple[bool, str]:
        """Run a single command over an open SSH connection. Returns (ok, output)."""
        result = await conn.run(command, check=False)
        output = (result.stdout or "") + (result.stderr or "")
        return result.exit_status == 0, output.strip()

    def _log_remote(self, message: str):
        logger.info(f"[{self.name}] {message}")
        asyncio.create_task(self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        ))

    async def _node_install(self, payload: dict) -> dict:
        """
        Install pip packages on a remote node via SSH.

        payload keys:
          host      — IP or hostname of the remote machine
          user      — SSH username (default: "pi")
          packages  — list of package names to install
          password  — SSH password (optional, prefer key auth)
          key_path  — path to SSH private key (optional)
        """
        try:
            import asyncssh
        except ImportError:
            return {"error": "asyncssh not installed. Run: pip install asyncssh"}

        host     = payload.get("host")
        packages = payload.get("packages", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]
        if not host:
            return {"error": "Missing 'host' in payload"}
        if not packages:
            return {"error": "No packages specified"}

        pkg_str = " ".join(packages)
        self._log_remote(f"Installing {pkg_str} on {host}...")

        try:
            async with asyncssh.connect(**self._ssh_kwargs(payload)) as conn:
                ok, output = await self._ssh_run(
                    conn,
                    f"pip install {pkg_str} --break-system-packages -q 2>&1"
                )
                if ok:
                    self._log_remote(f"✓ {pkg_str} installed on {host}")
                    return {"success": True, "host": host, "packages": packages, "output": output[-300:]}
                else:
                    self._log_remote(f"✗ Install failed on {host}: {output[-200:]}")
                    return {"success": False, "host": host, "error": output[-400:]}

        except Exception as e:
            return {"success": False, "host": host, "error": str(e)}

    async def _node_deploy(self, payload: dict) -> dict:
        """
        Full bootstrap of a new AgentFlow edge node via SSH.

        Steps:
          1. Create ~/agentflow/ directory
          2. Upload remote_runner.py
          3. Install aiomqtt (the only runtime dependency)
          4. Kill any existing runner with the same node name
          5. Start the runner in the background
          6. Verify it appears online within 15 seconds

        payload keys:
          host       — IP or hostname
          user       — SSH username (default: "pi")
          node_name  — name this node will use (default: "remote-node")
          broker     — MQTT broker host reachable FROM the Pi (default: "localhost")
          password   — SSH password (optional)
          key_path   — path to SSH private key (optional)
          port       — MQTT broker port (default: 1883)
        """
        try:
            import asyncssh
        except ImportError:
            return {"error": "asyncssh not installed. Run: pip install asyncssh"}

        host      = payload.get("host")
        user      = payload.get("user", "pi")
        node_name = payload.get("node_name", "remote-node")
        broker    = payload.get("broker", "localhost")
        mqtt_port = payload.get("port", 1883)

        if not host:
            return {"error": "Missing 'host' in payload"}

        # Find remote_runner.py relative to this file
        import pathlib
        candidates = [
            pathlib.Path(__file__).parent.parent / "remote_runner.py",
            pathlib.Path("remote_runner.py"),
            pathlib.Path(__file__).parent.parent.parent / "remote_runner.py",
        ]
        runner_path = next((p for p in candidates if p.exists()), None)
        if not runner_path:
            return {"error": "remote_runner.py not found. Make sure it is in the agentflow root."}

        self._log_remote(f"Deploying node '{node_name}' to {user}@{host}...")

        try:
            async with asyncssh.connect(**self._ssh_kwargs(payload)) as conn:

                # 1. Create directory
                await self._ssh_run(conn, "mkdir -p ~/agentflow")
                self._log_remote(f"[{node_name}] Directory created.")

                # 2. Upload remote_runner.py
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(runner_path), f"/home/{user}/agentflow/remote_runner.py")
                self._log_remote(f"[{node_name}] remote_runner.py uploaded.")

                # 3. Install the only required dependency
                ok, out = await self._ssh_run(
                    conn, "pip install aiomqtt --break-system-packages -q 2>&1"
                )
                if not ok:
                    self._log_remote(f"[{node_name}] pip install warning: {out[:150]}")
                else:
                    self._log_remote(f"[{node_name}] aiomqtt installed.")

                # 4. Kill any existing instance with this node name
                await self._ssh_run(
                    conn,
                    f"pkill -f 'remote_runner.py.*--name {node_name}' 2>/dev/null; true"
                )

                # 5. Start runner in the background
                cmd = (
                    f"nohup python3 ~/agentflow/remote_runner.py "
                    f"--broker {broker} --port {mqtt_port} --name {node_name} "
                    f"> ~/agentflow/{node_name}.log 2>&1 &"
                )
                await self._ssh_run(conn, cmd)
                self._log_remote(f"[{node_name}] Runner started.")

            self._log_remote(
                f"[{node_name}] Deploy complete! Node will appear in /nodes within 15s."
            )
            return {
                "success":   True,
                "node_name": node_name,
                "host":      host,
                "broker":    broker,
                "message":   (
                    f"Node '{node_name}' deployed to {user}@{host}. "
                    f"It will appear in /nodes within ~15 seconds."
                ),
            }

        except Exception as e:
            msg = f"Deploy failed for '{node_name}' on {host}: {e}"
            self._log_remote(msg)
            return {"success": False, "node_name": node_name, "host": host, "error": str(e)}

    async def _node_run(self, payload: dict) -> dict:
        """
        Run an arbitrary shell command on a remote node via SSH.

        payload keys:
          host     — IP or hostname
          user     — SSH username (default: "pi")
          command  — shell command to run
          password / key_path — auth (optional)
        """
        try:
            import asyncssh
        except ImportError:
            return {"error": "asyncssh not installed. Run: pip install asyncssh"}

        host    = payload.get("host")
        command = payload.get("command", "echo hello")
        if not host:
            return {"error": "Missing 'host' in payload"}

        self._log_remote(f"Running on {host}: {command[:80]}")
        try:
            async with asyncssh.connect(**self._ssh_kwargs(payload)) as conn:
                ok, output = await self._ssh_run(conn, command)
                return {
                    "success":   ok,
                    "host":      host,
                    "command":   command,
                    "output":    output,
                    "exit_code": 0 if ok else 1,
                }
        except Exception as e:
            return {"success": False, "host": host, "error": str(e)}
