"""Where an agent may read a person's documents from and write what it makes.

The paths come from requests a model wrote, so these tests pin the fence: the
document folders and chat uploads in, everything else out, and the state
directory out even when a document folder contains it.
"""

import os
import sys
from pathlib import Path

import pytest

from wactorz import documents
from wactorz.documents import (
    DOCUMENT_DIRS_ENV,
    DocumentRefused,
    default_documents_dir,
    document_dirs,
    readable_document,
    writable_document,
)
from wactorz.web.uploads import UPLOADS_DIRNAME

UPLOAD_ID = "0f3c9a5e4b2d4c1e8a7b6c5d4e3f2a1b"


@pytest.fixture(name="docs")
def docs_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    monkeypatch.setenv(DOCUMENT_DIRS_ENV, str(folder))
    return folder


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(documents.Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv(DOCUMENT_DIRS_ENV, raising=False)
    return home


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not let the test make a symlink")


class TestTheDocumentFolders:
    def test_unset_they_are_the_documents_folder(self, home: Path) -> None:
        assert document_dirs() == [default_documents_dir().resolve()]

    def test_outside_linux_that_is_documents_in_the_home_folder(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(documents.sys, "platform", "darwin")

        assert default_documents_dir() == home / "Documents"

    def test_on_linux_the_desktop_names_it(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A translated desktop keeps documents under another name."""
        monkeypatch.setattr(documents.sys, "platform", "linux")
        (home / ".config").mkdir()
        (home / ".config" / "user-dirs.dirs").write_text(
            '# written by xdg-user-dirs-update\nXDG_DOCUMENTS_DIR="$HOME/Έγγραφα"\n',
            encoding="utf-8",
        )

        assert default_documents_dir() == home / "Έγγραφα"

    def test_a_documents_folder_set_to_home_is_not_taken(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """That is how the desktop says there is none; it would open the whole home."""
        monkeypatch.setattr(documents.sys, "platform", "linux")
        (home / ".config").mkdir()
        (home / ".config" / "user-dirs.dirs").write_text(
            'XDG_DOCUMENTS_DIR="$HOME/"\n', encoding="utf-8"
        )

        assert default_documents_dir() == home / "Documents"

    def test_on_linux_without_the_desktops_file_it_is_documents(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(documents.sys, "platform", "linux")

        assert default_documents_dir() == home / "Documents"

    def test_several_are_named_the_way_path_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv(DOCUMENT_DIRS_ENV, f"{first}{os.pathsep}{os.pathsep}{second}")

        assert document_dirs() == [first.resolve(), second.resolve()]

    def test_a_blank_value_counts_as_unset(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DOCUMENT_DIRS_ENV, "  ")

        assert document_dirs() == [default_documents_dir().resolve()]


class TestReading:
    def test_a_file_in_a_document_folder(self, docs: Path) -> None:
        report = docs / "sub" / "report.txt"
        report.parent.mkdir()
        report.write_text("x", encoding="utf-8")

        assert readable_document(str(report)) == report.resolve()

    def test_a_file_elsewhere_is_refused_with_the_way_in(self, docs: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "secret.txt"
        elsewhere.write_text("x", encoding="utf-8")

        with pytest.raises(DocumentRefused, match=DOCUMENT_DIRS_ENV):
            readable_document(str(elsewhere))

    def test_climbing_out_is_refused(self, docs: Path, tmp_path: Path) -> None:
        (tmp_path / "secret.txt").write_text("x", encoding="utf-8")

        with pytest.raises(DocumentRefused, match="outside the document folders"):
            readable_document(str(docs / ".." / "secret.txt"))

    def test_a_link_out_of_a_document_folder_leads_nowhere(
        self, docs: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("x", encoding="utf-8")
        _link(docs / "innocent.txt", secret)

        with pytest.raises(DocumentRefused, match="outside the document folders"):
            readable_document(str(docs / "innocent.txt"))

    def test_the_state_directory_is_refused_inside_a_document_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pickles and keys live there; naming a parent folder must not open it."""
        monkeypatch.setenv(DOCUMENT_DIRS_ENV, str(tmp_path))
        pickle = tmp_path / "state" / "agent.pkl"
        pickle.parent.mkdir(exist_ok=True)
        pickle.write_bytes(b"x")

        with pytest.raises(DocumentRefused, match="state directory"):
            readable_document(str(pickle))

    def test_a_missing_file_is_said(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="File not found"):
            readable_document(str(docs / "gone.txt"))

    def test_a_folder_is_not_a_file(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="File not found"):
            readable_document(str(docs))

    def test_naming_nothing_is_said(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="Name the file"):
            readable_document()

    def test_a_path_with_a_null_byte_is_refused(self, docs: Path) -> None:
        """The operating system cannot take one, so it is refused, not passed on."""
        with pytest.raises(DocumentRefused, match="not a usable path"):
            readable_document(str(docs / "report.txt\0.pdf"))

    def test_a_link_that_leads_to_itself_is_refused(self, docs: Path) -> None:
        _link(docs / "loop.txt", docs / "loop.txt")

        with pytest.raises(DocumentRefused):
            readable_document(str(docs / "loop.txt"))


class TestUploads:
    @staticmethod
    def _upload(tmp_path: Path) -> Path:
        stored = tmp_path / "state" / UPLOADS_DIRNAME / UPLOAD_ID
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"%PDF-1.4")
        return stored

    def test_an_upload_is_read_by_its_id(self, docs: Path, tmp_path: Path) -> None:
        stored = self._upload(tmp_path)

        assert readable_document(file_id=UPLOAD_ID) == stored

    def test_the_id_wins_over_a_path(self, docs: Path, tmp_path: Path) -> None:
        stored = self._upload(tmp_path)

        assert readable_document("/etc/passwd", UPLOAD_ID) == stored

    @pytest.mark.parametrize("file_id", ["../../etc/passwd", "0f3c", UPLOAD_ID.upper()])
    def test_anything_but_an_id_is_refused(self, docs: Path, file_id: str) -> None:
        with pytest.raises(DocumentRefused, match="not an upload id"):
            readable_document(file_id=file_id)

    def test_an_upload_that_is_gone_is_said(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="No upload"):
            readable_document(file_id=UPLOAD_ID)

    def test_an_upload_is_never_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DOCUMENT_DIRS_ENV, str(tmp_path))
        self._upload(tmp_path)

        with pytest.raises(DocumentRefused, match="state directory"):
            writable_document(str(tmp_path / "state" / UPLOADS_DIRNAME / "deck.pptx"), ".pptx")


class TestWriting:
    def test_a_file_of_its_kind_in_a_document_folder(self, docs: Path) -> None:
        assert writable_document(str(docs / "deck.PPTX"), ".pptx") == (docs / "deck.PPTX").resolve()

    def test_another_kind_of_file_is_refused(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match=r"must end in \.pptx"):
            writable_document(str(docs / ".bashrc"), ".pptx")

    def test_a_folder_that_does_not_exist_is_not_made(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="No folder"):
            writable_document(str(docs / "new" / "deck.pptx"), ".pptx")

        assert not (docs / "new").exists()

    def test_a_path_with_a_null_byte_is_refused(self, docs: Path) -> None:
        with pytest.raises(DocumentRefused, match="not a usable path"):
            writable_document(str(docs / "deck\0.pptx"), ".pptx")

    def test_elsewhere_is_refused(self, docs: Path, tmp_path: Path) -> None:
        with pytest.raises(DocumentRefused, match="outside the document folders"):
            writable_document(str(tmp_path / "deck.pptx"), ".pptx")

    def test_a_link_out_is_not_written_through(self, docs: Path, tmp_path: Path) -> None:
        _link(docs / "deck.pptx", tmp_path / "elsewhere.pptx")

        with pytest.raises(DocumentRefused, match="outside the document folders"):
            writable_document(str(docs / "deck.pptx"), ".pptx")


@pytest.mark.skipif(sys.platform == "win32", reason="the home folder is spelled ~ on POSIX")
def test_a_home_relative_path_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(DOCUMENT_DIRS_ENV, "~/docs")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("x", encoding="utf-8")

    assert readable_document("~/docs/a.txt") == (tmp_path / "docs" / "a.txt").resolve()
