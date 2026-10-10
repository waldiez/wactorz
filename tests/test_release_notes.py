"""The GitHub release page shows a version's summary, not its whole section.

A CHANGELOG section opens with a summary -- a paragraph, ``Before you
upgrade`` and ``Highlights`` -- and then gives every change in full. The release
notes are the summary and a link to the rest; a section written without a
summary is still published whole, so no release page comes out empty.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent


def _load_script() -> ModuleType:
    """Import `scripts/release_notes.py`, which is not an installed module."""
    path = ROOT / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load_script()

URL = "https://github.com/waldiez/wactorz"

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not yet released.

## [0.7.0] - 2026-10-03

Wactorz 0.7.0 is about trust.

### Before you upgrade

- **Redeploy every edge node.**

### Highlights

- **Signed commands.**

### Added

- **Signed commands, in full.** A long account.

### Fixed

- **A fix, in full.**

## [0.6.0] - 2026-08-31

### Added

- **An older change.**

### Fixed

- **An older fix.**
"""


class TestASectionWithASummary:
    def test_the_notes_are_the_summary(self) -> None:
        text = release_notes.notes(CHANGELOG, "0.7.0", URL)

        assert text.startswith("Wactorz 0.7.0 is about trust.\n")
        assert "### Before you upgrade" in text
        assert "**Redeploy every edge node.**" in text
        assert "### Highlights" in text
        assert "**Signed commands.**" in text

    def test_the_full_account_is_left_out(self) -> None:
        text = release_notes.notes(CHANGELOG, "0.7.0", URL)

        assert "### Added" not in text
        assert "in full" not in text.split("---")[0]
        assert "### Fixed" not in text

    def test_it_links_to_the_full_section_at_the_tag(self) -> None:
        text = release_notes.notes(CHANGELOG, "0.7.0", URL)

        assert f"({URL}/blob/v0.7.0/CHANGELOG.md#070---2026-10-03)" in text

    def test_no_other_version_reaches_it(self) -> None:
        text = release_notes.notes(CHANGELOG, "0.7.0", URL)

        assert "not yet released" not in text
        assert "older" not in text


class TestASectionWithoutASummary:
    def test_it_is_published_whole(self) -> None:
        text = release_notes.notes(CHANGELOG, "0.6.0", URL)

        assert "**An older change.**" in text
        assert "**An older fix.**" in text

    def test_it_has_no_link_since_nothing_was_left_out(self) -> None:
        assert "CHANGELOG.md](" not in release_notes.notes(CHANGELOG, "0.6.0", URL)


class TestTheCommand:
    def test_a_version_with_no_section_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(CHANGELOG, encoding="utf-8")

        assert release_notes.main(["9.9.9", "--changelog", str(changelog)]) == 1
        assert "9.9.9" in capsys.readouterr().err

    def test_a_tag_name_is_accepted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(CHANGELOG, encoding="utf-8")

        assert release_notes.main(["v0.7.0", "--changelog", str(changelog), "--url", URL]) == 0
        assert capsys.readouterr().out.startswith("Wactorz 0.7.0 is about trust.")


def test_this_release_publishes_its_summary_only() -> None:
    # The release this script was written for: if its section loses its
    # summary, the release page goes back to the whole of it.
    text = release_notes.notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "0.7.0", URL)

    assert "### Highlights" in text
    assert "### Added" not in text
    assert "/blob/v0.7.0/CHANGELOG.md#070---2026-10-03" in text
