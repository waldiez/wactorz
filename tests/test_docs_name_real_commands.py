# SPDX-License-Identifier: Apache-2.0
"""The commands the docs tell a reader to run exist, and are the way to run them.

A doc that names a Makefile target nobody has, or a flag the parser drops, fails
silently: `make` says "No rule", and `wactorz` takes unknown flags without a
word (`parse_known_args`). Each check reads only code in the Markdown (spans and
fenced blocks), where a command is meant to be typed; prose says "make" too.

The changelog is left out: it records what was true when each entry was written.
"""

import argparse
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from wactorz.arguments import build_parser

ROOT = Path(__file__).resolve().parent.parent

#: Docs left out of every check, each with its reason.
_NOT_READ = {
    "CHANGELOG.md": "history: names what each entry's change replaced",
}

#: Where `bun run build` may stand alone: the frontend's own table of its scripts,
#: which describes the script rather than telling anyone to build with it.
_BUN_BUILD_ALONE = {"frontend/README.md"}

_FENCE = re.compile(r"^(```|~~~)([^\n]*)\n(.*?)^\1", re.MULTILINE | re.DOTALL)

#: Fenced blocks read as commands: shells, or no language named. A `text` block
#: is something else, such as a request typed into the chat.
_SHELLS = {"", "bash", "sh", "shell", "zsh", "console", "powershell", "pwsh", "ps1", "bat", "cmd"}
_SPAN = re.compile(r"`([^`\n]+)`")
_MAKE = re.compile(r"(?<![\w./-])make\s+(?:-[A-Za-z]+\s+)*(?:[A-Z_]+=\S*\s+)*([a-z][\w.-]*)")
_TARGET = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", re.MULTILINE)
_WACTORZ = re.compile(r"(?<![\w./~-])(?:python3?\s+-m\s+)?wactorz(?![\w.\[-])([^\n#|`&;]*)")
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def _docs() -> list[Path]:
    """Every Markdown file git tracks, less the ones `_NOT_READ` names."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    listed = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.split()
    return [ROOT / name for name in listed if name not in _NOT_READ]


def _blocks(text: str) -> list[str]:
    """The command blocks of a Markdown text, each whole, with shell comments taken out."""
    return [
        "\n".join(line.split("#", 1)[0] for line in found.group(3).splitlines()) + "\n"
        for found in _FENCE.finditer(text)
        if found.group(2).strip().lower() in _SHELLS
    ]


def _prose_lines(text: str) -> list[str]:
    """The text outside fenced blocks, one entry per line, with an indented line that
    carries on the one before it (a wrapped list item) joined to it.
    """
    lines: list[str] = []
    for line in _FENCE.sub("", text).splitlines():
        if lines and lines[-1].strip() and line[:1].isspace() and line.strip():
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    return lines


def _code(text: str) -> list[str]:
    """The code in a Markdown text: each command block whole, then each inline span."""
    prose = _FENCE.sub("", text)
    return _blocks(text) + [found.group(1) for found in _SPAN.finditer(prose)]


def _make_targets() -> set[str]:
    return set(_TARGET.findall((ROOT / "Makefile").read_text(encoding="utf-8")))


def _flags(parser: argparse.ArgumentParser) -> tuple[set[str], set[str]]:
    """The parser's flags: those it shows in `--help`, and those it takes but hides."""
    shown: set[str] = set()
    hidden: set[str] = set()
    for action in parser._actions:  # argparse offers no public list of them
        (hidden if action.help == argparse.SUPPRESS else shown).update(action.option_strings)
    return shown, hidden


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_every_make_target_named_in_the_docs_exists() -> None:
    targets = _make_targets()
    missing = [
        f"{_relative(doc)}: make {target}"
        for doc in _docs()
        for code in _code(doc.read_text(encoding="utf-8"))
        for target in _MAKE.findall(code)
        if target not in targets
    ]
    assert not missing, f"targets the Makefile does not have: {missing}"


def test_bun_run_build_is_given_beside_make_build_frontend() -> None:
    """`make build-frontend` also copies the build into an installed wactorz.

    `bun run build` is fine where `make` is not (PowerShell), so a doc may give
    it, but as the fallback in the same block or line as `make build-frontend`.
    """
    alone: list[str] = []
    for doc in _docs():
        if _relative(doc) in _BUN_BUILD_ALONE:
            continue
        text = doc.read_text(encoding="utf-8")
        # A block is read whole; inline code by the line it stands in.
        pieces = _blocks(text)
        pieces += [line for line in _prose_lines(text) if "`" in line]
        alone += [
            f"{_relative(doc)}: {piece.strip()[:80]}"
            for piece in pieces
            if "bun run build" in piece and "build-frontend" not in piece
        ]
    assert not alone, f"`bun run build` with no `make build-frontend` beside it: {alone}"


def test_every_wactorz_flag_named_in_the_docs_is_one_it_shows() -> None:
    """A flag the parser lacks is dropped without a word; a hidden one is there only so old
    launchers keep working, and is not to be taught (the token flags put a secret in `ps`).
    """
    shown, hidden = _flags(build_parser())
    wrong = [
        f"{_relative(doc)}: wactorz {flag}{' (hidden)' if flag in hidden else ''}"
        for doc in _docs()
        for code in _code(doc.read_text(encoding="utf-8"))
        for line in code.splitlines()
        for found in _WACTORZ.finditer(line)
        for flag in _FLAG.findall(found.group(1))
        if flag not in shown
    ]
    assert not wrong, f"flags `wactorz` does not show in --help: {wrong}"


# ── the checks themselves ───────────────────────────────────────────────────


def test_code_is_read_from_spans_and_blocks_and_not_from_prose() -> None:
    text = "Run `make lint` first.\n\n```bash\nmake test\n```\n\nThis does not make sense.\n"
    assert _code(text) == ["make test\n", "make lint"]


def test_a_wrapped_list_item_is_read_as_one_line() -> None:
    text = "- [ ] `make build-frontend`, or\n      `bun run build` without make\n- [ ] next\n"
    assert _prose_lines(text) == [
        "- [ ] `make build-frontend`, or `bun run build` without make",
        "- [ ] next",
    ]


def test_a_text_block_and_a_shell_comment_are_not_commands() -> None:
    text = "```text\nwhen the door opens, make reachy wake up\n```\n```bash\nmake lint  # then make sense\n```\n"
    assert [target for code in _code(text) for target in _MAKE.findall(code)] == ["lint"]


@pytest.mark.parametrize(
    ("code", "target"),
    [
        ("make lint", "lint"),
        ("make -s tool-image NAME=mosquitto", "tool-image"),
        ("make test-py-versions PYTHONS=3.11", "test-py-versions"),
        ("DURATION=60 make soak", "soak"),
    ],
)
def test_a_make_line_yields_its_target(code: str, target: str) -> None:
    assert _MAKE.findall(code) == [target]


def test_a_flag_is_read_after_wactorz_and_not_after_another_command() -> None:
    line = "wactorz --interface discord --discord-token x && wactorz-node --node edge"
    flags = [flag for found in _WACTORZ.finditer(line) for flag in _FLAG.findall(found.group(1))]
    assert flags == ["--interface", "--discord-token"]


def test_the_token_flags_count_as_hidden() -> None:
    shown, hidden = _flags(build_parser())
    assert {"--discord-token", "--telegram-token"} <= hidden
    assert "--interface" in shown
