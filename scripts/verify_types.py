# SPDX-License-Identifier: Apache-2.0
"""How complete the types are that wactorz offers its users, as a type checker sees them.

basedpyright's ``--verifytypes`` scores every symbol the package exports: known
type, ambiguous, or unknown. It reads an installed package with a ``py.typed``
marker, so this builds a scratch environment, installs the locked dependencies
and then wactorz itself (not editable: an editable install hides the package
from the check), and runs the check there with the basedpyright the
development environment has.

What is checked is what was installed, marker and all: a build that drops
``py.typed`` fails here, since without it users' type checkers read none of the
package's types.

The score is printed with what holds it back most, and the run fails below
``--floor``: a ratchet, raised as the score climbs and never lowered.
"""

import argparse
import collections
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

#: The extras installed with the package: every integration whose library's types the package exposes.
EXTRAS = ("all", "langgraph")

#: What the description of the command line is: the module docstring's first line.
SUMMARY = "How complete the types are that wactorz offers its users, as a type checker sees them."


class NoUvError(SystemExit):
    """uv, which builds the scratch environment, is not on PATH."""

    def __init__(self) -> None:
        super().__init__("uv is needed to build the scratch environment; see `make install-dev`")


class BelowFloorError(SystemExit):
    """A score under the floor the run was given."""

    def __init__(self, score: float, floor: float) -> None:
        super().__init__(f"Type completeness {score:.2f}% is below the floor of {floor:.2f}%.")


class NotTheInstallError(SystemExit):
    """The package found was not the one installed in the scratch environment."""

    def __init__(self, found: Path) -> None:
        super().__init__(f"wactorz was found at {found}, not in the scratch environment")


class NoMarkerError(SystemExit):
    """The installed package carries no ``py.typed``: the build dropped it."""

    def __init__(self, package: Path) -> None:
        super().__init__(
            f"{package} has no py.typed, so users' type checkers read none of its types; "
            "the build must include wactorz/py.typed"
        )


class UnreadablePackageError(SystemExit):
    """basedpyright could not read the installed package, and said why."""

    def __init__(self, why: str) -> None:
        super().__init__(f"verifytypes could not read the package: {why}")


def _run(*cmd: str, cwd: Path | None = None) -> str:
    """Run ``cmd``, returning what it printed, and stop with its output if it fails."""
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.stderr.write(done.stdout + done.stderr)
        raise SystemExit(done.returncode)
    return done.stdout


def _install(scratch: Path, uv: str) -> Path:
    """A scratch environment with the locked dependencies and wactorz in it; its Python."""
    venv = scratch / "venv"
    _run(uv, "venv", "--quiet", "--python", sys.executable, str(venv))
    python = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
    requirements = scratch / "requirements.txt"
    # With the extras a user installs for the integrations: without them, the
    # types the package takes from those libraries read as unknown here though
    # a user who has them installed sees them.
    _run(
        uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
        *(f"--extra={extra}" for extra in EXTRAS),
        "--output-file", str(requirements), cwd=REPO,
    )  # fmt: skip
    _run(uv, "pip", "install", "--quiet", "--python", str(python), "-r", str(requirements))
    # The package without its dependencies, which are the locked ones above; the
    # checker with its own, which it needs to run.
    _run(uv, "pip", "install", "--quiet", "--python", str(python), "--no-deps", str(REPO))
    checker = f"basedpyright=={importlib.metadata.version('basedpyright')}"
    _run(uv, "pip", "install", "--quiet", "--python", str(python), checker)
    # Isolated, and from the scratch directory: from the checkout, `import wactorz`
    # would find the source tree in the working directory and look at that instead.
    package = Path(
        _run(
            str(python),
            "-I",
            "-c",
            "import pathlib, wactorz; print(pathlib.Path(wactorz.__file__).parent)",
            cwd=scratch,
        ).strip()
    )
    if not package.is_relative_to(scratch):
        raise NotTheInstallError(package)
    if not (package / "py.typed").is_file():
        raise NoMarkerError(package)
    return python


def _completeness(python: Path, scratch: Path) -> dict[str, Any]:
    """The type-completeness report of the installed package."""
    checker = python.parent / "basedpyright"
    # From an empty directory, so no configuration nearby changes what is read.
    empty = scratch / "run"
    empty.mkdir()
    # The scratch environment as the one active: basedpyright finds its packages
    # through the active environment's Python, which would otherwise be the
    # development one, where wactorz is the editable install it cannot read.
    venv = python.parent.parent
    env = {
        **os.environ,
        "VIRTUAL_ENV": str(venv),
        "PATH": os.pathsep.join([str(python.parent), os.environ.get("PATH", "")]),
    }
    done = subprocess.run(
        [str(checker), "--verifytypes", "wactorz", "--ignoreexternal", "--outputjson"],
        cwd=empty, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    try:
        report = json.loads(done.stdout)
    except json.JSONDecodeError:
        said = (done.stdout + done.stderr).strip()
        raise UnreadablePackageError(said or f"exit {done.returncode}") from None
    problems = [
        d["message"] for d in report.get("generalDiagnostics", []) if d.get("severity") == "error"
    ]
    if problems:
        raise UnreadablePackageError("; ".join(problems))
    return report["typeCompleteness"]


def _summary(completeness: dict[str, Any], top: int) -> str:
    """The score, the counts, and where the unknowns are."""
    counts = completeness["exportedSymbolCounts"]
    by_package: collections.Counter[str] = collections.Counter()
    by_reason: collections.Counter[str] = collections.Counter()
    for symbol in completeness["symbols"]:
        if symbol.get("isExported") and not symbol.get("isTypeKnown"):
            by_package[".".join(symbol["name"].split(".")[:2])] += 1
            for diagnostic in symbol.get("diagnostics", [])[:1]:
                by_reason[diagnostic["message"].split("\n")[0].split('"')[0].strip()] += 1
    lines = [
        f"Type completeness: {completeness['completenessScore'] * 100:.2f}%  "
        f"(known {counts['withKnownType']}, ambiguous {counts['withAmbiguousType']}, "
        f"unknown {counts['withUnknownType']})",
        "Most incomplete:",
        *(f"  {n:5d}  {name}" for name, n in by_package.most_common(top)),
        "Why:",
        *(f"  {n:5d}  {reason}" for reason, n in by_reason.most_common(top)),
    ]
    return "\n".join(lines)


def main() -> None:
    """Score the installed package and fail below the floor."""
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument(
        "--floor", type=float, default=0.0, help="the least score, in percent, that passes"
    )
    parser.add_argument("--top", type=int, default=10, help="how many packages and reasons to list")
    parser.add_argument(
        "--json", type=Path, help="also write the whole report here, every symbol in it"
    )
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise NoUvError()
    with tempfile.TemporaryDirectory(prefix="wactorz-verifytypes-") as tmp:
        scratch = Path(tmp)
        completeness = _completeness(_install(scratch, uv), scratch)
    print(_summary(completeness, args.top))
    if args.json is not None:
        args.json.write_text(json.dumps(completeness, indent=1))
    score = completeness["completenessScore"] * 100
    if score < args.floor:
        raise BelowFloorError(score, args.floor)


if __name__ == "__main__":
    main()
