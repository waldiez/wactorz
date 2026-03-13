#!/usr/bin/env python3
"""
Build script for AgentFlow — produces a PyPI-ready wheel and sdist.

Usage:
    python scripts/build.py           # build only
    python scripts/build.py --upload  # build + upload to PyPI

Environment variables:
    TWINE_USERNAME / TWINE_PASSWORD   — PyPI credentials (or use ~/.pypirc)
    AGENTFLOW_FRONTEND_STALE=0        — force frontend rebuild (set by this script)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def run(cmd: list[str], **kwargs) -> None:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    upload = "--upload" in sys.argv

    # ── Clean previous build artefacts ───────────────────────────────────────
    for d in ("dist", "build"):
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p)
            print(f"  removed {d}/")

    # ── Install build tools ───────────────────────────────────────────────────
    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
         "hatchling", "twine"])

    # ── Build wheel + sdist (force fresh frontend via STALE_AFTER=0) ─────────
    env = {**os.environ, "AGENTFLOW_FRONTEND_STALE": "0"}
    run([sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-t", "sdist"],
        cwd=ROOT, env=env)

    # ── Validate packages ─────────────────────────────────────────────────────
    run([sys.executable, "-m", "twine", "check", "dist/*"], cwd=ROOT, shell=False)
    # twine check doesn't support globs on Windows; pass files explicitly
    dist_files = list((ROOT / "dist").iterdir())
    run([sys.executable, "-m", "twine", "check"] + [str(f) for f in dist_files])

    print("\n  Packages:")
    for f in sorted(dist_files):
        size_kb = f.stat().st_size // 1024
        print(f"    {f.name}  ({size_kb} kB)")

    # ── Upload ────────────────────────────────────────────────────────────────
    if upload:
        run([sys.executable, "-m", "twine", "upload"] + [str(f) for f in dist_files])
    else:
        print("\n  Run with --upload to publish to PyPI.")


if __name__ == "__main__":
    main()
