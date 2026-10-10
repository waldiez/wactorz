"""A state directory in one archive, and back.

An export takes what the system remembers -- its database, each agent's state
and blobs, the install's identity and keys -- and leaves out what is
regenerated or would be stale. An import puts it back, but only into a
directory nothing is running on, and only an archive that holds exactly the
files it lists, each as listed, at a path inside the directory: its pickles are
unpickled when the system starts.
"""

import hashlib
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.persistence.pickle_store import PickleStore
from wactorz.core.state_lock import LOCK_FILE, StateLock
from wactorz.state_archive import (
    ArchiveExists,
    ArchiveInsideState,
    DamagedArchive,
    DirectoryInUse,
    DirectoryNotEmpty,
    NotAnArchive,
    export_state,
    import_state,
    main,
)

#: What is exported, by path, and what is left out of every archive.
KEPT = {
    "install_id": b"abc123",
    "known_hosts": b"rpi ssh-ed25519 AAAA",
    "node_signing.key": b"k" * 64,
    "mqtt_tls/ca.crt": b"CERT",
    "mqtt_tls/ca.key": b"PRIVATE",
    "uploads/a1b2.png": b"\x89PNG",
    "weather/blobs/model-abc.blob": b"\x00\x01",
}
LEFT_OUT = {
    LOCK_FILE: b"1",
    "sessions.json": b"{}",
    "wactorz.log": b"log",
    "mqtt_outbox.db": b"queued",
    "..incoming-blobs/" + "a" * 64: b"arriving",
    "weather/.state.pkl.123.abc.tmp": b"half",
    "weather/state.pkl.corrupt.1700000000": b"bad",
}
SECRETS = {"node_signing.key", "mqtt_tls/ca.key"}


def _state(directory: Path) -> Path:
    """A state directory as a running server leaves it."""
    for relative, data in {**KEPT, **LEFT_OUT}.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    store = PickleStore(str(directory))
    store.update("weather", "count", 7)
    store.flush()
    with sqlite3.connect(directory / "wactorz.db") as db:
        db.execute("CREATE TABLE kv (k TEXT, v TEXT)")
        db.execute("INSERT INTO kv VALUES ('greeting', 'hello')")
    return directory


def _tree(directory: Path) -> set[str]:
    return {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}


def _archive(path: Path, members: dict[str, bytes], listed: dict[str, bytes] | None = None) -> Path:
    """An archive holding ``members``, whose manifest lists ``listed`` (default: the same)."""
    files = {
        name.removeprefix("state/"): {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in (members if listed is None else listed).items()
    }
    manifest = json.dumps({"format": 1, "secrets": True, "files": files}).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, data in {**members, "manifest.json": manifest}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


class TestExport:
    def test_what_is_kept_and_what_is_left_out(self, tmp_path: Path) -> None:
        source = _state(tmp_path / "state")

        done = export_state(tmp_path / "out.tar.gz", source)

        with tarfile.open(done.path) as tar:
            names = {m.name.removeprefix("state/") for m in tar if m.name != "manifest.json"}
        assert names == {*KEPT, "weather/state.pkl", "wactorz.db"}
        assert sorted(done.secret_files) == sorted(SECRETS)

    def test_without_secrets_the_keys_stay_behind(self, tmp_path: Path) -> None:
        source = _state(tmp_path / "state")

        done = export_state(tmp_path / "out.tar.gz", source, secrets=False)

        with tarfile.open(done.path) as tar:
            names = {m.name.removeprefix("state/") for m in tar}
            manifest = json.loads(tar.extractfile("manifest.json").read())  # pyright: ignore[reportOptionalMemberAccess]
        assert not names & SECRETS
        assert "mqtt_tls/ca.crt" in names, "the certificate is not a secret"
        assert manifest["secrets"] is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_the_archive_is_its_owners_alone(self, tmp_path: Path) -> None:
        done = export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))

        assert done.path.stat().st_mode & 0o777 == 0o600

    def test_the_database_is_copied_whole_while_it_is_open(self, tmp_path: Path) -> None:
        source = _state(tmp_path / "state")
        live = sqlite3.connect(source / "wactorz.db")
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO kv VALUES ('late', 'still here')")
        live.commit()
        try:
            export_state(tmp_path / "out.tar.gz", source)
        finally:
            live.close()

        restored = tmp_path / "restored"
        import_state(tmp_path / "out.tar.gz", restored)
        with sqlite3.connect(restored / "wactorz.db") as db:
            rows = dict(db.execute("SELECT k, v FROM kv").fetchall())
        assert rows == {"greeting": "hello", "late": "still here"}

    def test_a_file_named_like_a_database_that_is_not_one_is_kept_as_it_is(
        self, tmp_path: Path
    ) -> None:
        source = _state(tmp_path / "state")
        (source / "notes.db").write_bytes(b"not sqlite at all")
        export_state(tmp_path / "out.tar.gz", source)

        import_state(tmp_path / "out.tar.gz", tmp_path / "restored")

        assert (tmp_path / "restored" / "notes.db").read_bytes() == b"not sqlite at all"

    def test_it_is_not_written_into_the_directory_it_holds(self, tmp_path: Path) -> None:
        source = _state(tmp_path / "state")

        with pytest.raises(ArchiveInsideState):
            export_state(source / "out.tar.gz", source)

    def test_it_does_not_overwrite_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "out.tar.gz").write_bytes(b"precious")

        with pytest.raises(ArchiveExists):
            export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))
        assert (tmp_path / "out.tar.gz").read_bytes() == b"precious"


class TestImport:
    def test_the_state_comes_back_as_it_was(self, tmp_path: Path) -> None:
        source = _state(tmp_path / "state")
        export_state(tmp_path / "out.tar.gz", source)
        restored = tmp_path / "restored"

        done = import_state(tmp_path / "out.tar.gz", restored)

        assert _tree(restored) == {*KEPT, "weather/state.pkl", "wactorz.db"}
        for relative, data in KEPT.items():
            assert (restored / relative).read_bytes() == data
        assert PickleStore(str(restored)).load("weather") == {"count": 7}
        assert done.secrets and done.previous is None

    def test_not_while_something_runs_on_the_directory(self, tmp_path: Path) -> None:
        export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))
        running = StateLock(tmp_path / "target")
        running.acquire()
        try:
            with pytest.raises(DirectoryInUse):
                import_state(tmp_path / "out.tar.gz", tmp_path / "target", replace=True)
        finally:
            running.release()

    def test_not_over_a_directory_with_something_in_it(self, tmp_path: Path) -> None:
        export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("mine")

        with pytest.raises(DirectoryNotEmpty):
            import_state(tmp_path / "out.tar.gz", target)
        assert _tree(target) == {"keep.txt"}

    def test_replacing_keeps_what_was_there_beside_it(self, tmp_path: Path) -> None:
        export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("mine")

        done = import_state(tmp_path / "out.tar.gz", target, replace=True)

        assert done.previous is not None and (done.previous / "keep.txt").read_text() == "mine"
        assert "install_id" in _tree(target)

    def test_a_lock_nobody_holds_does_not_count_as_something_there(self, tmp_path: Path) -> None:
        export_state(tmp_path / "out.tar.gz", _state(tmp_path / "state"))
        target = tmp_path / "target"
        target.mkdir()
        (target / LOCK_FILE).write_text("12345")

        done = import_state(tmp_path / "out.tar.gz", target)

        assert done.previous is None


def _refused(tmp_path: Path, archive: Path, error: type[Exception]) -> None:
    """The archive is refused, and nothing is left behind or put in place."""
    target = tmp_path / "target"
    with pytest.raises(error):
        import_state(archive, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.import-*")), "the half-written import is removed"


class TestArchivesThatAreRefused:
    def test_one_whose_file_was_changed(self, tmp_path: Path) -> None:
        archive = _archive(
            tmp_path / "a.tar.gz",
            {"state/weather/state.pkl": b"altered"},
            listed={"state/weather/state.pkl": b"original"},
        )
        _refused(tmp_path, archive, DamagedArchive)

    @pytest.mark.parametrize(
        "name", ["state/../escape", "/etc/passwd", "state/a\\..\\..\\b", "elsewhere/x", "state/c:x"]
    )
    def test_one_with_a_path_outside_the_directory(self, tmp_path: Path, name: str) -> None:
        _refused(tmp_path, _archive(tmp_path / "a.tar.gz", {name: b"x"}), DamagedArchive)

    def test_one_with_a_link(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.tar.gz"
        manifest = json.dumps({"format": 1, "files": {}}).encode()
        with tarfile.open(archive, "w:gz") as tar:
            link = tarfile.TarInfo("state/weather/state.pkl")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
        _refused(tmp_path, archive, DamagedArchive)

    def test_one_with_a_file_it_does_not_list(self, tmp_path: Path) -> None:
        archive = _archive(
            tmp_path / "a.tar.gz",
            {"state/install_id": b"x", "state/main/state.pkl": b"extra"},
            listed={"state/install_id": b"x"},
        )
        _refused(tmp_path, archive, DamagedArchive)

    def test_one_missing_a_file_it_lists(self, tmp_path: Path) -> None:
        archive = _archive(
            tmp_path / "a.tar.gz",
            {"state/install_id": b"x"},
            listed={"state/install_id": b"x", "state/wactorz.db": b"db"},
        )
        _refused(tmp_path, archive, DamagedArchive)

    @pytest.mark.parametrize("manifest", [None, b"not json", b'{"format": 99, "files": {}}'])
    def test_one_that_is_not_a_state_archive(self, tmp_path: Path, manifest: Any) -> None:
        archive = tmp_path / "a.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            if manifest is not None:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(manifest)
                tar.addfile(info, io.BytesIO(manifest))
        _refused(tmp_path, archive, NotAnArchive)


class TestTheCommand:
    def test_export_then_import(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        source = _state(tmp_path / "state")
        archive = tmp_path / "out.tar.gz"

        assert main(["export", str(archive), "--state-dir", str(source)]) == 0
        assert "keys" in caplog.text
        assert main(["import", str(archive), "--state-dir", str(tmp_path / "restored")]) == 0
        assert "install_id" in _tree(tmp_path / "restored")

    def test_a_refusal_is_said_and_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert main(["import", str(tmp_path / "missing.tar.gz")]) == 1
        assert "wactorz-state:" in caplog.text
