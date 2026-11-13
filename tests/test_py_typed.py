# SPDX-License-Identifier: Apache-2.0
"""wactorz says it is typed: the `py.typed` marker is there, and nothing keeps it out of the wheel.

Without the marker a user's type checker reads none of the package's types
(PEP 561). `make verifytypes` builds and installs the package and fails when the
install lacks it; this checks, without a build, the three things that would
drop it from one.
"""

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
MARKER = REPO / "wactorz" / "py.typed"


def _wheel() -> dict[str, Any]:
    """The wheel's build settings in pyproject.toml."""
    # tomllib is in the standard library from Python 3.11.
    tomllib = pytest.importorskip("tomllib")
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return project["tool"]["hatch"]["build"]["targets"]["wheel"]


def test_the_marker_is_there() -> None:
    assert MARKER.is_file()


def test_git_does_not_ignore_it() -> None:
    # The build leaves out what git ignores, and so would a commit.
    ignored = subprocess.run(["git", "check-ignore", "--quiet", str(MARKER)], cwd=REPO, check=False)
    assert ignored.returncode == 1, "wactorz/py.typed is ignored by git"


def test_the_wheel_packages_wactorz() -> None:
    assert "wactorz" in _wheel()["packages"]


def test_no_exclude_pattern_matches_it() -> None:
    relative = MARKER.relative_to(REPO).as_posix()
    matched = [
        pattern
        for pattern in _wheel().get("exclude", [])
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(MARKER.name, pattern)
    ]
    assert matched == []
