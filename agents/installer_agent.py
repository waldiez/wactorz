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
import subprocess

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
    #"duckduckgo-search": "duckduckgo_search",
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
        """Run pip install using subprocess in a thread (Windows-compatible).

        Using run_in_executor + subprocess.run instead of create_subprocess_exec
        because Windows SelectorEventLoop doesn't support async subprocesses,
        and ProactorEventLoop breaks MQTT's add_reader/add_writer.
        """
        cmd = [sys.executable, "-m", "pip", "install", package]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")

        def _run_pip():
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                output = result.stdout + result.stderr
                return result.returncode == 0, output
            except subprocess.TimeoutExpired:
                return False, "Timed out after 180s"
            except Exception as e:
                return False, f"subprocess error: {e}"

        try:
            loop = asyncio.get_running_loop()
            success, output = await loop.run_in_executor(None, _run_pip)

            if success:
                importlib.invalidate_caches()

            return success, output

        except Exception as e:
            logger.error(f"[{self.name}] _pip_install failed for {package}: {e!r}")
            return False, f"executor error: {e}"

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