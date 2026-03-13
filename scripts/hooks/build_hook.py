"""
Hatchling pre-build hook — ensures the Vite frontend is built before packaging.

If ``frontend/dist/index.html`` is missing or older than STALE_AFTER seconds
the hook runs ``npm ci && npm run build`` (or ``bun`` / ``yarn`` / ``pnpm``
if the project is configured for them) before hatchling assembles the wheel.

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
STALE_AFTER: int = int(os.getenv("AGENTFLOW_FRONTEND_STALE", "600"))


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
        root       = Path(self.root)
        frontend   = root / "frontend"
        dist_index = root / "frontend" / "dist" / "index.html"

        if not _is_stale(dist_index):
            self.app.display_info(
                f"[build-hook] frontend/dist is fresh — skipping rebuild"
            )
            return

        if not frontend.is_dir():
            self.app.display_warning(
                "[build-hook] frontend/ directory not found — skipping build"
            )
            return

        pm = _pkg_manager(frontend)
        self.app.display_info(f"[build-hook] building frontend with {pm[0]} …")

        def _run(cmd: list[str]) -> None:
            result = subprocess.run(cmd, cwd=frontend, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"[build-hook] command failed (exit {result.returncode}): "
                    + " ".join(cmd)
                )

        try:
            _run(pm + ["install", "--frozen-lockfile"] if pm[0] != "npm"
                 else pm + ["ci"])
            _run(pm + ["run", "build"])
        except RuntimeError as exc:
            self.app.display_error(str(exc))
            sys.exit(1)

        self.app.display_info("[build-hook] frontend build complete ✓")
