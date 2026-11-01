"""Write the command table in `docs/reference.md` from what `/help` lists.

The table sits between two markers, and everything outside them is written by
hand. A command's words come from its own forms, so a command is documented by
giving it forms, and a change shows in `/help` and the docs together.
`tests/test_command_docs.py` fails while the table is out of date.

    python scripts/sync_command_docs.py           # rewrite the table
    python scripts/sync_command_docs.py --check   # exit 1 if it is out of date
"""

import argparse
import sys
from pathlib import Path

from wactorz.agents.main.commands import registry
from wactorz.agents.main.commands.dispatch import Form
from wactorz.agents.main.commands.info import help_sections

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "reference.md"
START = "<!-- commands:start: written by scripts/sync_command_docs.py from the commands' forms -->"
END = "<!-- commands:end -->"


class MissingMarkersError(ValueError):
    """The page has no place marked for the table."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path}: no {START!r} … {END!r} block to write the table into")


def _cell(text: str) -> str:
    """Text for one table cell: a pipe would end the cell, even inside backticks."""
    return text.replace("|", "\\|")


def _row(form: Form) -> str:
    summary = form.summary[:1].upper() + form.summary[1:]
    return f"| `{_cell(form.usage)}` | {_cell(summary)} |"


def render_table(sections: list[tuple[str, list[Form]]]) -> str:
    """The marked block: one table per `/help` heading, in `/help`'s order."""
    lines = [START]
    for group, forms in sections:
        lines += ["", f"#### {group}", "", "| Command | What it does |", "|---|---|"]
        lines += [_row(form) for form in forms]
    lines += ["", END]
    return "\n".join(lines)


def with_table(page: str, table: str, path: Path = REFERENCE) -> str:
    """``page`` with its marked block replaced by ``table``."""
    head, start, rest = page.partition(START)
    _, end, tail = rest.partition(END)
    if not start or not end:
        raise MissingMarkersError(path)
    return head + table + tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the reference's command table.")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if stale")
    args = parser.parse_args(argv)
    page = REFERENCE.read_text(encoding="utf-8")
    updated = with_table(page, render_table(help_sections(registry)))
    if updated == page:
        return 0
    if args.check:
        print(
            f"{REFERENCE.relative_to(ROOT)}: the command table is out of date; "
            "run python scripts/sync_command_docs.py",
            file=sys.stderr,
        )
        return 1
    REFERENCE.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
