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

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Set to 0 to force a rebuild regardless of what changed (release/CI). Any
# other value leaves the decision to source mtimes — see _is_stale. The name
# and the legacy default are kept so existing invocations keep working.
STALE_AFTER: int = int(os.getenv("WACTORZ_FRONTEND_STALE", "600"))


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Never invoke a JS toolchain — use the committed static/app as-is.
SKIP_BUILD = _flag("WACTORZ_SKIP_FRONTEND_BUILD")
# Make a missing toolchain / failed build fatal (for release & CI builds).
# Default is non-fatal so `pip install` never fails for lack of bun/node/npm.
STRICT = _flag("WACTORZ_FRONTEND_STRICT")


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


# Files whose contents end up in the bundle. Anything outside these cannot
# change the build output, so it must not trigger a rebuild.
_SOURCE_GLOBS: tuple[str, ...] = (
    "src/**/*",
    "index.html",
    "package.json",
    "bun.lock",
    "vite.config.ts",
    "tsconfig.json",
)


# Where the fingerprint of the last successful build is remembered. Inside
# node_modules so it is already ignored by git and never reaches the wheel;
# losing it (a wiped node_modules) costs one redundant rebuild, nothing more.
_FINGERPRINT_REL = Path("node_modules") / ".cache" / "wactorz-frontend-inputs"


def _input_fingerprint(frontend: Path) -> str:
    """SHA-256 over the contents of every file that can affect the bundle."""
    digest = hashlib.sha256()
    files = sorted(
        p for pattern in _SOURCE_GLOBS for p in frontend.glob(pattern) if p.is_file()
    )
    for path in files:
        digest.update(path.relative_to(frontend).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_stale(dist_index: Path, frontend: Path) -> bool:
    """Whether ``static/app`` needs rebuilding — i.e. an input actually changed.

    Compares a content hash of the inputs against the one recorded by the last
    successful build. Neither timestamp scheme this replaced was trustworthy:
    wall-clock age answers "when was this built", not "is it still correct", so
    it rebuilt after any idle period (rewriting ``static/app`` and dirtying the
    tree for no reason) while missing edits made just after a build; and mtime
    ordering breaks on a fresh clone, where checkout stamps sources and dist
    alike with the current time, and fires for a ``touch`` that changed nothing.
    Content is the thing we actually care about, so compare that.
    """
    if not dist_index.exists():
        return True
    if STALE_AFTER == 0:
        return True  # forced rebuild (release/CI)
    if not frontend.is_dir():
        return False  # no sources to compare against; keep what is committed
    recorded = frontend / _FINGERPRINT_REL
    try:
        return recorded.read_text().strip() != _input_fingerprint(frontend)
    except OSError:
        return True  # never built here, or unreadable — build and record


def _record_fingerprint(frontend: Path) -> None:
    """Remember the inputs that produced the current ``static/app``."""
    target = frontend / _FINGERPRINT_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_input_fingerprint(frontend))
    except OSError:
        pass  # a cache we cannot write is a slower build, not a failure


class CustomBuildHook(BuildHookInterface):
    """Ensure the Vite frontend is built before the wheel is assembled."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        self._build_frontend(Path(self.root))

    def _build_frontend(self, root: Path) -> None:
        frontend = root / "frontend"
        dist_index = root / "static" / "app" / "index.html"

        if SKIP_BUILD:
            self.app.display_info(
                "[build-hook] WACTORZ_SKIP_FRONTEND_BUILD set — using committed static/app"
            )
            return

        if not _is_stale(dist_index, frontend):
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
            msg = (
                f"[build-hook] {pm[0]} not found and static/app/index.html is missing. "
                f"Install {pm[0]} (or bun/pnpm/npm) to build the frontend."
            )
            if STRICT:
                self.app.display_error(msg)
                sys.exit(1)
            self.app.display_warning(msg + " — continuing without the bundled SPA")
            return

        self.app.display_info(f"[build-hook] building frontend with {pm[0]} …")

        def _run(cmd: list[str]) -> None:
            result = subprocess.run(cmd, cwd=frontend, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"[build-hook] command failed (exit {result.returncode}): " + " ".join(cmd)
                )

        try:
            _run(pm + ["install", "--frozen-lockfile"] if pm[0] != "npm" else pm + ["ci"])
            _run(pm + ["run", "build"])
        except RuntimeError as exc:
            if STRICT:
                self.app.display_error(str(exc))
                sys.exit(1)
            self.app.display_warning(str(exc) + " — continuing with existing static/app")
            return

        _record_fingerprint(frontend)
        self.app.display_info("[build-hook] frontend build complete ✓")
