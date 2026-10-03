"""Write the GitHub release notes for one version from CHANGELOG.md.

    python3 scripts/release_notes.py 0.7.0 --url https://github.com/waldiez/wactorz

A version's CHANGELOG section opens with a summary for people deciding whether
to upgrade: a paragraph, then ``### Before you upgrade`` and ``### Highlights``.
The full account of every change follows under ``### Added``, ``### Changed``
and the rest, and is long enough that a release page carrying all of it is
read by nobody. So the notes are the summary alone, ending in a link to the
full section in the CHANGELOG at the release's tag.

A section with no ``### Highlights`` has no summary to stop at, and is copied
whole, as the release page always showed it.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent

#: The headings that make up the summary. Any other ``###`` after
#: ``### Highlights`` starts the full account, which the notes leave out.
SUMMARY_HEADINGS = ("### Before you upgrade", "### Highlights")


def section(changelog: str, version: str) -> list[str]:
    """The lines under ``## [version]``, up to the next version's heading."""
    lines: list[str] = []
    found = False
    for line in changelog.splitlines():
        if line.startswith(f"## [{version}]"):
            found = True
            continue
        if found and line.startswith("## ["):
            break
        if found:
            lines.append(line)
    return lines


def summary(lines: Sequence[str]) -> tuple[list[str], bool]:
    """The summary part of a section, and whether anything was left out."""
    seen_highlights = False
    for index, line in enumerate(lines):
        if line.startswith("### Highlights"):
            seen_highlights = True
        elif seen_highlights and line.startswith("### ") and not line.startswith(SUMMARY_HEADINGS):
            return list(lines[:index]), True
    return list(lines), False


def changelog_anchor(heading: str) -> str:
    """The fragment GitHub gives a Markdown heading: ``## [0.7.0] - 2026-10-03``."""
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def notes(changelog: str, version: str, url: str = "") -> str:
    """The release notes for ``version``; empty when it has no section."""
    lines = section(changelog, version)
    if not any(line.strip() for line in lines):
        return ""
    kept, cut = summary(lines)
    while kept and not kept[-1].strip():
        kept.pop()
    while kept and not kept[0].strip():
        kept.pop(0)
    text = "\n".join(kept) + "\n"
    if cut and url:
        heading = next(
            line for line in changelog.splitlines() if line.startswith(f"## [{version}]")
        )
        link = f"{url}/blob/v{version}/CHANGELOG.md#{changelog_anchor(heading)}"
        text += f"\n---\n\nEvery change in this release, in full: [CHANGELOG.md]({link})\n"
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the GitHub release notes for one version.")
    parser.add_argument("version", help="the version, with or without a leading v")
    parser.add_argument(
        "--url", default="", help="the repository's URL, for the link to the full section"
    )
    parser.add_argument("--changelog", type=Path, default=ROOT_DIR / "CHANGELOG.md")
    args = parser.parse_args(argv)

    version = args.version.removeprefix("v")
    text = notes(args.changelog.read_text(encoding="utf-8"), version, args.url.rstrip("/"))
    if not text:
        print(f"No CHANGELOG.md section found for {version}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
