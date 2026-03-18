"""
Hatchling pre-build hook — ensures the Vite frontend is built before packaging.

``static/app/`` (Vite SPA) and ``static/docs/`` (docs site) are committed
to the repository and bundled into the wheel as-is — no build tools are
required at install time.  The hook only rebuilds ``static/app/`` if it
is missing or stale.

Configure in pyproject.toml:

    [tool.hatch.build.hooks.custom]
    path = "scripts/hooks/build_hook.py"
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Rebuild if the dist is older than this (seconds).  Set to 0 in CI.
STALE_AFTER: int = int(os.getenv("WACTORZ_FRONTEND_STALE", "600"))


def _pkg_manager(frontend_dir: Path) -> list[str]:
    """Return the detected package-manager command prefix."""
    pkg_json = frontend_dir / "package.json"
    try:
        import json
        data = json.loads(pkg_json.read_text())
        pm = data.get("packageManager", "")
        if pm.startswith("bun"):
            return ["bun"]
        if pm.startswith("pnpm"):
            return ["pnpm"]
        if pm.startswith("yarn"):
            return ["yarn"]
    except Exception:
        pass
    return ["npm"]


def _pm_available(pm: list[str]) -> bool:
    """Return True if the package-manager executable is on PATH."""
    import shutil
    return shutil.which(pm[0]) is not None


def _is_stale(dist_index: Path) -> bool:
    if not dist_index.exists():
        return True
    if STALE_AFTER == 0:
        return True          # always rebuild when STALE_AFTER=0 (CI)
    age = time.time() - dist_index.stat().st_mtime
    return age > STALE_AFTER


class CustomBuildHook(BuildHookInterface):
    """Ensure the Vite frontend is built before the wheel is assembled."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        self._build_frontend(Path(self.root))

    def _build_frontend(self, root: Path) -> None:
        frontend   = root / "frontend"
        dist_index = root / "static" / "app" / "index.html"

        if not _is_stale(dist_index):
            self.app.display_info("[build-hook] static/app is fresh — skipping rebuild")
            return

        if not frontend.is_dir():
            self.app.display_warning("[build-hook] frontend/ not found — skipping")
            return

        pm = _pkg_manager(frontend)

        if not _pm_available(pm):
            if dist_index.exists():
                self.app.display_info(
                    f"[build-hook] {pm[0]} not found — "
                    "static/app already present (committed), skipping rebuild"
                )
                return
            self.app.display_error(
                f"[build-hook] {pm[0]} not found and static/app/index.html is missing. "
                f"Install {pm[0]} (or bun/pnpm/npm) to build the frontend."
            )
            sys.exit(1)

        self.app.display_info(f"[build-hook] building frontend with {pm[0]} …")

        def _run(cmd: list[str]) -> None:
            result = subprocess.run(cmd, cwd=frontend, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"[build-hook] command failed (exit {result.returncode}): "
                    + " ".join(cmd)
                )

        try:
            _run(pm + ["install", "--frozen-lockfile"] if pm[0] != "npm" else pm + ["ci"])
            _run(pm + ["run", "build"])
        except RuntimeError as exc:
            self.app.display_error(str(exc))
            sys.exit(1)

        self.app.display_info("[build-hook] frontend build complete ✓")
