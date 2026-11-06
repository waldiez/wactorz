#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Every source file opens with the project's license line.

The line is `SPDX-License-Identifier: Apache-2.0`, in the file's own comment
syntax. It goes first, after only what has to come before it: a shebang, or an
HTML doctype. The copyright is stated once, in NOTICE.md, which the license
carries to anyone who redistributes the work; a file repeats only the license,
so no file changes when a year does. The frontend's eslint `headers/header-format`
rule asks the same of TypeScript.

A file that names another license in its own SPDX line keeps it: that is a
decision about the file (an example built on AGPL code, say), not a missing
notice.

Without paths, every file git tracks or would add with a covered suffix is read, and the
built dashboard in `static/app` with them. With paths, only those, which is how
the commit hook calls it. `--fix` writes the line where it is missing; a built
file is never edited, since the next build would undo it, so it is reported
with the command that rebuilds it.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LICENSE = "Apache-2.0"
SPDX_LINE = f"SPDX-License-Identifier: {LICENSE}"

#: Suffixes whose comments start with `#`.
HASH_SUFFIXES = frozenset({".py", ".sh", ".ps1"})
#: Suffixes that take a `/** ... */` block.
BLOCK_SUFFIXES = frozenset({".ts", ".js", ".mjs", ".css"})
HTML_SUFFIXES = frozenset({".html"})
SUFFIXES = HASH_SUFFIXES | BLOCK_SUFFIXES | HTML_SUFFIXES

#: Committed build output: checked, never edited.
BUILT = Path("static/app")
#: Committed build output that is not ours to stamp: the docs site's generated pages.
NOT_CHECKED = (Path("static/docs"),)

#: The license line in each comment form, at the top of a file. The licence it
#: names is captured: a file under another one keeps it.
OPENING_RES = {
    "hash": re.compile(r"# SPDX-License-Identifier: (\S+)\s*"),
    "block": re.compile(r" \* SPDX-License-Identifier: (\S+)\s*"),
    "html": re.compile(r"<!-- SPDX-License-Identifier: (\S+) -->\s*"),
}
#: A per-file copyright line under the license line: NOTICE.md holds it.
OLD_COPYRIGHT_RE = re.compile(r"\s*(?:#|\*)\s*Copyright \d{4}(?: - \d{4})? Waldiez\b.*\n?")


def notice_block(suffix: str) -> str:
    """The license line as a comment of the language `suffix` names, ending in a newline."""
    if suffix in HASH_SUFFIXES:
        return f"# {SPDX_LINE}\n"
    if suffix in HTML_SUFFIXES:
        return f"<!-- {SPDX_LINE} -->\n"
    return f"/**\n * {SPDX_LINE}\n */\n"


def preamble_length(lines: Sequence[str], suffix: str) -> int:
    """How many leading lines must stay above the notice: a shebang or a doctype."""
    if not lines:
        return 0
    first = lines[0].lstrip("\ufeff")
    if suffix in HASH_SUFFIXES and first.startswith("#!"):
        return 1
    if suffix in HTML_SUFFIXES and first.lower().startswith("<!doctype"):
        return 1
    return 0


def opening_license(lines: Sequence[str], suffix: str) -> tuple[int, str] | None:
    """Where the file's opening license line is and the license it names, if it opens with one.

    Opening means first, after only a shebang or a doctype, in the file's own
    comment form; for a block, the first line inside the `/**` (or `/*`) that
    starts the file. A license named anywhere else does not count.
    """
    lines = [lines[0].lstrip("\ufeff"), *lines[1:]] if lines else []
    at = preamble_length(lines, suffix)
    if suffix in HASH_SUFFIXES:
        form = "hash"
    elif suffix in HTML_SUFFIXES:
        form = "html"
    else:
        if at >= len(lines) or lines[at].rstrip() not in ("/*", "/**"):
            return None
        at += 1
        form = "block"
    found = OPENING_RES[form].fullmatch(lines[at]) if at < len(lines) else None
    return (at, found.group(1)) if found else None


def problem(text: str, suffix: str) -> str | None:
    """What is wrong with the license line in `text`, or None when nothing is."""
    lines = text.splitlines(keepends=True)
    opening = opening_license(lines, suffix)
    if opening is None:
        return "does not open with the license line"
    at, license_id = opening
    if license_id != LICENSE:
        return None
    if at + 1 < len(lines) and OLD_COPYRIGHT_RE.fullmatch(lines[at + 1]):
        return "repeats the copyright under its license line (NOTICE.md holds it)"
    return None


def with_notice(text: str, suffix: str) -> str:
    """`text` opening with the license line, and no copyright line under it."""
    lines = text.splitlines(keepends=True)
    opening = opening_license(lines, suffix)
    if opening is not None:
        if problem(text, suffix) is not None:
            del lines[opening[0] + 1]
        return "".join(lines)
    keep = preamble_length(lines, suffix)
    if keep and not lines[keep - 1].endswith("\n"):
        lines[keep - 1] += "\n"
    return "".join([*lines[:keep], notice_block(suffix), *lines[keep:]])


def is_built(path: Path) -> bool:
    return path.is_relative_to(BUILT)


def covered(path: Path) -> bool:
    """Whether `path` (relative to the root) is a file this check reads."""
    if path.suffix not in SUFFIXES:
        return False
    return not any(path.is_relative_to(skip) for skip in NOT_CHECKED)


def source_files(include_new: bool = True) -> list[Path]:
    """Every file git tracks, and every new one it does not ignore, relative to the root.

    New files count: a file about to be added is held to the rule before the
    commit hook meets it, as ruff holds it to its own. `include_new=False` gives
    the tracked ones alone.
    """
    new = ["--others", "--exclude-standard"] if include_new else []
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", *new],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    return [Path(name) for name in out.split("\0") if name]


def built_files() -> list[Path]:
    """What the last frontend build wrote, tracked or not yet."""
    return sorted(p.relative_to(ROOT) for p in (ROOT / BUILT).rglob("*") if p.is_file())


def files_to_read(paths: Iterable[str]) -> list[Path]:
    """The files a run reads: those named, or the whole tree."""
    named = [Path(p) for p in paths]
    if named:
        chosen = [p.resolve().relative_to(ROOT) if p.is_absolute() else p for p in named]
    else:
        chosen = sorted({*source_files(), *built_files()})
    return [p for p in chosen if covered(p) and (ROOT / p).is_file()]


def run(paths: Iterable[str], fix: bool) -> int:
    """Check (and with `fix`, repair) the files; the process's exit code."""
    failed = False
    for path in files_to_read(paths):
        file = ROOT / path
        text = file.read_text(encoding="utf-8")
        wrong = problem(text, path.suffix)
        if wrong is None:
            continue
        if fix and not is_built(path):
            file.write_text(with_notice(text, path.suffix), encoding="utf-8", newline="")
            print(f"{path}: license line written")
            continue
        failed = True
        hint = " (built: run `make build-frontend`)" if is_built(path) else ""
        print(f"{path}: {wrong}{hint}")
    if failed and not fix:
        print("Run `make fmt-licenses` to write them.", file=sys.stderr)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every source file opens with the license line."
    )
    parser.add_argument(
        "--fix", action="store_true", help="write the license line where it is missing"
    )
    parser.add_argument(
        "paths", nargs="*", help="files to read (default: every tracked source file)"
    )
    args = parser.parse_args(argv)
    return run(args.paths, args.fix)


if __name__ == "__main__":
    sys.exit(main())
