"""Recognising a file dropped onto the terminal, and deciding whether to take it.

A terminal turns a drop into a paste of the file's path, so most of the rules
here are about telling that apart from text someone pasted. The rest apply the
dashboard's own accept-list and size cap, so the TUI takes exactly what the
browser would.
"""

# pylint: disable=missing-function-docstring

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from wactorz import config
from wactorz.tui import attachments
from wactorz.tui.attachments import Staged
from wactorz.web import uploads

PDF = b"%PDF-1.7\n" + b"\x00" * 40


@pytest.fixture(name="pdf")
def pdf_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(PDF)
    return path


# ── recognising a drop ──────────────────────────────────────────────────────


def test_an_absolute_path_is_a_drop(pdf: Path) -> None:
    assert attachments.paths_in_paste(str(pdf)) == [pdf]


def test_a_quoted_path_with_spaces_is_one_file(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    assert attachments.paths_in_paste(f"'{target}'") == [target]


def test_a_backslash_escaped_space_is_part_of_the_name(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    assert attachments.paths_in_paste(str(target).replace(" ", "\\ ")) == [target]


def test_a_file_uri_is_decoded(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    assert attachments.paths_in_paste("file://" + str(target).replace(" ", "%20")) == [target]


def test_a_home_relative_path_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert attachments.paths_in_paste("~/notes.md") == [tmp_path / "notes.md"]


def test_several_files_can_arrive_in_one_paste(tmp_path: Path) -> None:
    a, b = tmp_path / "a.pdf", tmp_path / "b.png"
    assert attachments.paths_in_paste(f"{a} {b}") == [a, b]
    assert attachments.paths_in_paste(f"{a}\n{b}\n") == [a, b]


@pytest.mark.parametrize(
    "text",
    ["hello world", "README.md", "see /tmp/x.pdf please", "", "   ", "'unbalanced /tmp/x"],
    ids=["prose", "relative", "path-in-prose", "empty", "blank", "unbalanced-quote"],
)
def test_ordinary_text_is_not_a_drop(text: str) -> None:
    assert attachments.paths_in_paste(text) is None


# ── deciding what to take ───────────────────────────────────────────────────


def test_an_accepted_file_is_staged_with_its_size(pdf: Path) -> None:
    assert attachments.examine([pdf]) == ([Staged(path=pdf, name="report.pdf", size=len(PDF))], [])


def test_a_path_that_is_not_a_file_makes_the_paste_text(tmp_path: Path, pdf: Path) -> None:
    assert attachments.examine([pdf, tmp_path / "missing.pdf"]) is None
    assert attachments.examine([tmp_path]) is None


def test_an_unsupported_type_is_skipped_with_a_reason(tmp_path: Path) -> None:
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 10)
    assert attachments.examine([exe]) == ([], ["tool.exe (unsupported type)"])


def test_a_file_over_the_limit_is_skipped(monkeypatch: pytest.MonkeyPatch, pdf: Path) -> None:
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 10)
    assert attachments.examine([pdf]) == ([], ["report.pdf (over 10 B)"])


def test_an_empty_file_is_skipped(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert attachments.examine([empty]) == ([], ["empty.txt (empty)"])


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("photo.png", True),
        ("voice.mp3", True),
        ("notes.txt", True),
        ("notes.md", True),
        ("data.csv", True),
        ("data.json", True),
        ("deck.pptx", True),
        ("sheet.xlsx", True),
        ("report.PDF", True),
        ("tool.exe", False),
        ("archive.zip", False),
    ],
)
def test_types_follow_the_dashboard_accept_list(name: str, accepted: bool) -> None:
    assert attachments.accepted_type(Path("/x") / name) is accepted


# ── storing and describing ──────────────────────────────────────────────────


def test_a_staged_file_is_stored_as_an_upload(tmp_path: Path, pdf: Path) -> None:
    with patch.object(uploads, "resolve_state_dir", return_value=str(tmp_path)):
        record = attachments.store_staged(Staged(path=pdf, name="report.pdf", size=len(PDF)))
        file_id = str(record["id"])
        assert uploads.metadata(file_id) == {
            "id": file_id,
            "name": "report.pdf",
            "mime": "application/pdf",
            "size": len(PDF),
        }
        assert uploads.read_bytes(file_id) == PDF


@pytest.mark.parametrize(
    ("item", "shown"),
    [
        ({"name": "t.txt", "size": 12}, "📎 t.txt (12 B)"),
        ({"name": "a.pdf", "size": 2048}, "📎 a.pdf (2 KB)"),
        ({"name": "big.wav", "size": 5 * 1024 * 1024}, "📎 big.wav (5.0 MB)"),
        ({"name": "a.pdf", "size": 0}, "📎 a.pdf"),
        ({"size": "junk"}, "📎 attachment"),
        ({"name": "flag", "size": True}, "📎 flag"),
    ],
)
def test_an_attachment_is_described_for_the_transcript(item: dict[str, Any], shown: str) -> None:
    assert attachments.describe(item) == shown
