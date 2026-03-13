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
    """Ensure the Vite frontend and docs site are built before the wheel is assembled."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        self._build_frontend(root)
        self._build_docs(root)

    def _build_frontend(self, root: Path) -> None:
        frontend   = root / "frontend"
        dist_index = frontend / "dist" / "index.html"

        if not _is_stale(dist_index):
            self.app.display_info("[build-hook] frontend/dist is fresh — skipping rebuild")
            return

        if not frontend.is_dir():
            self.app.display_warning("[build-hook] frontend/ not found — skipping")
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
            _run(pm + ["install", "--frozen-lockfile"] if pm[0] != "npm" else pm + ["ci"])
            _run(pm + ["run", "build"])
        except RuntimeError as exc:
            self.app.display_error(str(exc))
            sys.exit(1)

        self.app.display_info("[build-hook] frontend build complete ✓")

    def _build_docs(self, root: Path) -> None:
        site_index = root / "site" / "index.html"

        if not _is_stale(site_index):
            self.app.display_info("[build-hook] site/ is fresh — skipping docs rebuild")
            return

        build_script = root / "scripts" / "build_docs.py"
        if not build_script.exists():
            self.app.display_warning("[build-hook] scripts/build_docs.py not found — using placeholder")
            self._ensure_docs_placeholder(root)
            return

        self.app.display_info("[build-hook] building docs …")
        result = subprocess.run(
            [sys.executable, str(build_script)],
            cwd=root, check=False,
        )
        if result.returncode != 0:
            self.app.display_warning("[build-hook] docs build failed — using placeholder")
            self._ensure_docs_placeholder(root)
            return

        self.app.display_info("[build-hook] docs build complete ✓")

    @staticmethod
    def _ensure_docs_placeholder(root: Path) -> None:
        """Create a minimal site/ so force-include doesn't fail."""
        site = root / "site"
        site.mkdir(exist_ok=True)
        placeholder = site / "index.html"
        if not placeholder.exists():
            placeholder.write_text(
                '<meta http-equiv="refresh" content="0; '
                'url=https://waldiez.github.io/agentflow/">\n'
            )
