#!/usr/bin/env python3
"""Lint the agent programs that are held in strings.

Most of the catalogue agent keeps its program in ``AGENT_CODE`` and execs
it at spawn. Ruff and the formatter see a string literal, so none of the
project's rules reach that code. This writes each program to a temporary file,
runs ruff over it with this repository's configuration, and reports findings
against the real file and line so the output can be followed back.

    python scripts/lint_agent_code.py            # report
    python scripts/lint_agent_code.py --fix      # apply ruff's safe fixes
    python scripts/lint_agent_code.py --format   # apply the formatter

A fix or a format is written back into the string it came from, so the module
around it is untouched.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "wactorz" / "catalogue_agents"
CONFIG = ROOT / "pyproject.toml"

#: The attribute the loader asks for; see `CatalogAgent._load_recipe`.
NAME = "AGENT_CODE"

#: Ruff resolves configuration from the working directory when a file has no
#: ancestor config, and a temporary file has none. Without this the programs
#: would be checked against ruff's defaults rather than this project's rules,
#: quietly, and the report would depend on where it was run from.
BASE = ["--config", str(CONFIG), "--no-cache"]

_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+): (?P<rest>.*)$")


@dataclass(frozen=True)
class Program:
    """One agent's source, and where it sits in the module that carries it."""

    path: Path
    source: str
    #: Line in `path` holding the program's first line, so a finding at program
    #: line 1 is reported against this.
    offset: int

    @property
    def name(self) -> str:
        return self.path.name


def _ruff() -> str:
    """The ruff this project uses, not whatever is on PATH."""
    candidate = Path(sys.executable).parent / "ruff"
    return str(candidate) if candidate.exists() else "ruff"


def programs() -> list[Program]:
    """Every agent program, found the way the loader finds it."""
    found: list[Program] = []
    for path in sorted(AGENTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        module = ast.parse(text)
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if not any(isinstance(t, ast.Name) and t.id == NAME for t in node.targets):
                continue
            # The string starts on the line after the opening quotes.
            found.append(Program(path=path, source=value.value, offset=node.lineno + 1))
    return found


def _write_back(program: Program, source: str) -> None:
    """Put an edited program back inside the string it came from."""
    text = program.path.read_text(encoding="utf-8")
    if text.count(program.source) != 1:
        raise SystemExit(f"{program.name}: program is not uniquely locatable; refusing to write")
    program.path.write_text(text.replace(program.source, source, 1), encoding="utf-8")


def _still_loads(source: str, label: str) -> None:
    """A program that no longer parses is worse than a lint finding."""
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise SystemExit(
            f"{label}: rewriting produced invalid Python at line {exc.lineno}"
        ) from exc


def check(program: Program, *, fix: bool, fmt: bool) -> tuple[int, str]:
    """Run ruff over one program. Returns (findings, possibly-rewritten source)."""
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory) / program.name
        scratch.write_text(program.source, encoding="utf-8")

        if fmt:
            subprocess.run(
                [_ruff(), "format", str(scratch), *BASE], capture_output=True, check=False
            )
        if fix:
            subprocess.run(
                [_ruff(), "check", str(scratch), "--fix", *BASE], capture_output=True, check=False
            )

        result = subprocess.run(
            [_ruff(), "check", str(scratch), "--output-format=concise", *BASE],
            capture_output=True,
            text=True,
            check=False,
        )
        edited = scratch.read_text(encoding="utf-8")
        shutil.copyfile(scratch, f".local/{program.name}")

    count = 0
    for line in result.stdout.splitlines():
        match = _FINDING.match(line)
        if not match:
            continue
        count += 1
        real = int(match["line"]) + program.offset - 1
        print(f"  {program.name}:{real}:{match['col']}: {match['rest']}")
    return count, edited


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="apply ruff's safe fixes")
    parser.add_argument("--format", dest="fmt", action="store_true", help="apply the formatter")
    parser.add_argument("agent", nargs="?", help="limit to one module, e.g. manual_agent.py")
    args = parser.parse_args()

    found = [p for p in programs() if not args.agent or p.name == args.agent]
    if not found:
        print(f"no {NAME} found under {AGENTS}", file=sys.stderr)
        return 1

    total = 0
    for program in found:
        count, edited = check(program, fix=args.fix, fmt=args.fmt)
        total += count
        if (args.fix or args.fmt) and edited != program.source:
            _still_loads(edited, program.name)
            _write_back(program, edited)
            print(f"  {program.name}: rewritten")

    print(f"\n{total} finding(s) across {len(found)} program(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
