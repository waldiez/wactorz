"""Models kept once by content, named by version, on main.

An artifact is a file someone put in the store on purpose, for agents to load.
These pin what the store promises: a name and a version resolve to one hash, the
same bytes are kept once, a directory hashes the same wherever it is, what runs
code when it loads is refused unless allowed, and nothing half-written is left
to look stored.
"""

import hashlib
import io
import json
import logging
import os
import tarfile
import threading
from pathlib import Path

import pytest

from wactorz import artifacts_cli
from wactorz.core import artifacts as artifacts_module
from wactorz.core.artifacts import (
    STORE_DIRNAME,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    describe,
    parse_reference,
)
from wactorz.core.state_lock import StateLock
from wactorz.state_archive import export_state, import_state

MODEL = b"\x08\x07onnx-ish bytes"


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(tmp_path / "state")


@pytest.fixture(name="model")
def model_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "detector.onnx"
    path.write_bytes(MODEL)
    return path


def a_directory(root: Path, *, order: tuple[str, ...] = ("a", "b")) -> Path:
    """A model in several files, created in ``order``."""
    contents = {"a": b"weights", "b": b"labels"}
    (root / "sub").mkdir(parents=True)
    for name in order:
        (root / ("sub" if name == "b" else ".") / f"{name}.bin").write_bytes(contents[name])
    return root


class TestAdding:
    def test_it_is_kept_under_its_hash_as_version_one(
        self, store: ArtifactStore, model: Path
    ) -> None:
        artifact = store.add(model, "imu-detector")

        assert artifact.reference == "imu-detector:1"
        assert artifact.sha256 == hashlib.sha256(MODEL).hexdigest()
        assert (artifact.format, artifact.size, artifact.runs_code) == ("onnx", len(MODEL), False)
        assert store.path_of(artifact).read_bytes() == MODEL
        assert artifact.pinned == f"imu-detector:1@{artifact.sha256}"

    def test_each_add_is_the_next_version(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu-detector")
        model.write_bytes(b"retrained")

        second = store.add(model, "imu-detector")

        assert second.version == 2
        assert [a.version for a in store.versions("imu-detector")] == [1, 2]

    def test_the_same_bytes_are_kept_once(self, store: ArtifactStore, model: Path) -> None:
        first = store.add(model, "imu-detector")
        again = store.add(model, "imu-detector")
        other = store.add(model, "copy")

        assert first.sha256 == again.sha256 == other.sha256
        assert [p.name for p in store.objects.iterdir()] == [first.sha256]

    def test_what_it_was_said_to_be_is_kept(self, store: ArtifactStore, model: Path) -> None:
        artifact = store.add(model, "imu-detector", metadata={"dataset": "imu-2026", "auc": 0.93})

        assert store.resolve("imu-detector:1").metadata == {"dataset": "imu-2026", "auc": 0.93}
        assert artifact.metadata["auc"] == 0.93

    def test_metadata_that_is_not_json_is_refused(self, store: ArtifactStore, model: Path) -> None:
        with pytest.raises(ArtifactError, match="metadata"):
            store.add(model, "imu-detector", metadata={"when": object()})

        assert store.names() == []

    @pytest.mark.parametrize("name", ["", "a/b", "a b", "a:b", "a@b", "-lead", "x" * 129, "a#"])
    def test_a_name_no_topic_or_reference_could_carry_is_refused(
        self, store: ArtifactStore, model: Path, name: str
    ) -> None:
        with pytest.raises(ArtifactError, match="usable name"):
            store.add(model, name)

    def test_a_missing_file_is_refused(self, store: ArtifactStore, tmp_path: Path) -> None:
        with pytest.raises(ArtifactError, match="does not exist"):
            store.add(tmp_path / "nothing.onnx", "imu-detector")


class TestFormats:
    @pytest.mark.parametrize(
        ("ending", "expected"),
        [
            (".onnx", "onnx"),
            (".safetensors", "safetensors"),
            (".npz", "npz"),
            (".NPY", "npy"),
            (".gguf", "gguf"),
            (".tflite", "tflite"),
            (".skops", "skops"),
        ],
    )
    def test_the_ending_says_the_format(
        self, store: ArtifactStore, tmp_path: Path, ending: str, expected: str
    ) -> None:
        path = tmp_path / f"model{ending}"
        path.write_bytes(b"x")

        assert store.add(path, "m").format == expected

    def test_an_ending_that_does_not_say_must_be_named(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # A .pt holds a state_dict or a whole module, and only one runs code.
        path = tmp_path / "model.pt"
        path.write_bytes(b"x")

        with pytest.raises(ArtifactError, match="does not say its format"):
            store.add(path, "m")
        assert store.add(path, "m", format_name="state_dict").format == "state_dict"

    def test_a_format_that_is_not_one_is_refused(self, store: ArtifactStore, model: Path) -> None:
        with pytest.raises(ArtifactError, match="not a format"):
            store.add(model, "m", format_name="h5")

    @pytest.mark.parametrize(
        ("ending", "format_name"), [(".pkl", None), (".joblib", None), (".pt", "torch")]
    )
    def test_what_runs_code_is_refused_unless_allowed(
        self, store: ArtifactStore, tmp_path: Path, ending: str, format_name: str | None
    ) -> None:
        path = tmp_path / f"model{ending}"
        path.write_bytes(b"x")

        with pytest.raises(ArtifactError, match="runs code"):
            store.add(path, "m", format_name=format_name)
        allowed = store.add(path, "m", format_name=format_name, allow_code=True)

        assert allowed.runs_code is True
        assert store.resolve("m").runs_code is True


class TestDirectories:
    def test_a_directory_is_kept_as_one_archive_of_it(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        artifact = store.add(a_directory(tmp_path / "model"), "yolo")

        with tarfile.open(store.path_of(artifact)) as tar:
            names = tar.getnames()
            weights = tar.extractfile("a.bin")
            assert weights is not None and weights.read() == b"weights"
        assert artifact.format == "dir"
        assert names == ["a.bin", "sub", "sub/b.bin"]

    def test_the_same_directory_hashes_the_same_wherever_it_is(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        here = a_directory(tmp_path / "here", order=("a", "b"))
        there = a_directory(tmp_path / "there", order=("b", "a"))
        os.utime(there / "a.bin", (1, 1))
        (there / "a.bin").chmod(0o600)

        assert store.add(here, "yolo").sha256 == store.add(there, "yolo").sha256

    def test_a_link_inside_is_refused(self, store: ArtifactStore, tmp_path: Path) -> None:
        directory = a_directory(tmp_path / "model")
        try:
            (directory / "outside").symlink_to(tmp_path)
        except OSError as exc:
            # Windows lets only an administrator, or Developer Mode, make one.
            pytest.skip(f"this machine cannot make a link: {exc}")

        with pytest.raises(ArtifactError, match="is a link"):
            store.add(directory, "yolo")
        assert store.names() == []
        assert not list(store.objects.glob("*"))

    def test_a_file_format_for_a_directory_is_refused(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        with pytest.raises(ArtifactError, match="directory"):
            store.add(a_directory(tmp_path / "model"), "yolo", format_name="onnx")

    def test_the_directory_format_for_a_file_is_refused(
        self, store: ArtifactStore, model: Path
    ) -> None:
        with pytest.raises(ArtifactError, match="not a directory"):
            store.add(model, "m", format_name="dir")


class TestResolving:
    @pytest.mark.parametrize(
        ("reference", "version"), [("imu", 2), ("imu:latest", 2), ("imu:1", 1), ("imu:2", 2)]
    )
    def test_a_reference_names_one_version(
        self, store: ArtifactStore, model: Path, reference: str, version: int
    ) -> None:
        store.add(model, "imu")
        store.add(model, "imu")

        assert store.resolve(reference).version == version

    @pytest.mark.parametrize("reference", ["imu:3", "other", "other:1"])
    def test_what_is_not_there_is_not_found(
        self, store: ArtifactStore, model: Path, reference: str
    ) -> None:
        store.add(model, "imu")

        with pytest.raises(ArtifactNotFoundError):
            store.resolve(reference)

    @pytest.mark.parametrize("reference", ["imu:0", "imu:-1", "imu:x", "imu:1.5", "bad name:1"])
    def test_a_reference_that_is_not_one_is_refused(self, reference: str) -> None:
        with pytest.raises(ArtifactError):
            parse_reference(reference)

    def test_an_empty_store_has_nothing(self, store: ArtifactStore) -> None:
        assert store.names() == []
        assert store.versions("imu") == []

    def test_an_index_this_release_cannot_read_is_refused(
        self, store: ArtifactStore, model: Path
    ) -> None:
        store.add(model, "imu")
        store.index_path.write_text(json.dumps({"format": 99, "artifacts": {}}), encoding="utf-8")

        with pytest.raises(ArtifactError, match="not one this release reads"):
            store.names()


class TestRemoving:
    def test_one_version_goes_and_the_rest_stay(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")
        model.write_bytes(b"second")
        second = store.add(model, "imu")

        (removed,) = store.remove("imu:1")

        assert removed.version == 1
        assert store.versions("imu") == [second]

    def test_its_file_goes_once_nothing_holds_it(self, store: ArtifactStore, model: Path) -> None:
        first = store.add(model, "imu")
        store.add(model, "copy")

        store.remove("imu:1")
        assert store.path_of(first).exists(), "another name still holds the same bytes"

        store.remove("copy:1")
        assert not store.path_of(first).exists()

    def test_a_bare_name_removes_every_version_only_when_asked(
        self, store: ArtifactStore, model: Path
    ) -> None:
        store.add(model, "imu")
        store.add(model, "imu")

        with pytest.raises(ArtifactError, match="every version"):
            store.remove("imu")
        assert len(store.remove("imu", every_version=True)) == 2
        assert store.names() == []

    def test_every_version_with_a_version_named_is_refused(
        self, store: ArtifactStore, model: Path
    ) -> None:
        store.add(model, "imu")

        with pytest.raises(ArtifactError):
            store.remove("imu:1", every_version=True)
        assert store.versions("imu")

    def test_latest_removes_the_newest(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")
        store.add(model, "imu")

        (removed,) = store.remove("imu:latest")

        assert removed.version == 2

    def test_what_is_not_there_is_not_found(self, store: ArtifactStore, model: Path) -> None:
        store.add(model, "imu")

        with pytest.raises(ArtifactNotFoundError):
            store.remove("imu:7")


class TestVerifying:
    def test_a_stored_file_matches(self, store: ArtifactStore, model: Path) -> None:
        artifact = store.add(model, "imu")

        assert store.verify(artifact) is None

    def test_a_changed_file_is_named(self, store: ArtifactStore, model: Path) -> None:
        artifact = store.add(model, "imu")
        store.path_of(artifact).write_bytes(b"tampered")

        problem = store.verify(artifact)

        assert problem is not None and "imu:1" in problem and "SHA-256" in problem

    def test_a_missing_file_is_named(self, store: ArtifactStore, model: Path) -> None:
        artifact = store.add(model, "imu")
        store.path_of(artifact).unlink()

        assert store.verify(artifact) is not None


class TestNothingHalfWritten:
    def test_a_copy_that_fails_leaves_nothing_that_looks_stored(
        self, store: ArtifactStore, model: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fails(_self: object, _data: object) -> int:
            raise OSError("the disk is full")

        monkeypatch.setattr(artifacts_module._Hashing, "write", fails)

        with pytest.raises(OSError, match="disk is full"):
            store.add(model, "imu")

        assert store.names() == []
        assert list(store.objects.iterdir()) == []

    def test_adds_at_the_same_time_each_get_a_version(
        self, store: ArtifactStore, model: Path
    ) -> None:
        # Each takes the store's lock to change the index, so none is lost.
        threads = [threading.Thread(target=store.add, args=(model, "imu")) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert [a.version for a in store.versions("imu")] == list(range(1, 9))


class TestKeptWithTheState:
    def test_an_export_takes_the_store_and_an_import_brings_it_back(
        self, tmp_path: Path, model: Path
    ) -> None:
        source = tmp_path / "state"
        artifact = ArtifactStore.in_state_dir(source).add(model, "imu")

        export_state(tmp_path / "state.tar", source)
        import_state(tmp_path / "state.tar", tmp_path / "restored")

        restored = ArtifactStore.in_state_dir(tmp_path / "restored")
        assert restored.resolve("imu:1") == artifact
        assert restored.verify(artifact) is None

    def test_it_never_shares_a_directory_with_an_agent(self) -> None:
        # An agent's name may not start with `..`, so none is ever called this.
        assert STORE_DIRNAME.startswith("..")


class TestTheCommand:
    """What the command says goes to its log, and `show` writes JSON to stdout."""

    @pytest.fixture(autouse=True)
    def _capture(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.artifacts_cli")
        self.caplog = caplog

    def run(self, *argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
        self.caplog.clear()
        code = artifacts_cli.main(list(argv))
        return code, capsys.readouterr().out + self.caplog.text

    def test_add_list_show_remove(
        self, tmp_path: Path, model: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = str(tmp_path / "state")

        code, out = self.run(
            "add", str(model), "imu", "--meta", "auc=0.93", "--meta", "dataset=imu-2026",
            "--state-dir", state, capsys=capsys,
        )  # fmt: skip
        assert code == 0 and "Stored imu:1@" in out

        code, out = self.run("list", "--state-dir", state, capsys=capsys)
        assert code == 0 and "imu:1" in out and "onnx" in out

        self.caplog.clear()
        assert artifacts_cli.main(["show", "imu:latest", "--state-dir", state]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["reference"] == "imu:1"
        assert shown["metadata"] == {"source": "cli", "auc": 0.93, "dataset": "imu-2026"}

        code, out = self.run("remove", "imu:1", "--state-dir", state, capsys=capsys)
        assert code == 0 and "Removed imu:1" in out
        assert ArtifactStore.in_state_dir(state).names() == []

    def test_a_refusal_is_said_and_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pickled = tmp_path / "model.pkl"
        pickled.write_bytes(b"x")
        state = str(tmp_path / "state")

        code, out = self.run("add", str(pickled), "m", "--state-dir", state, capsys=capsys)
        assert code == 1 and "runs code" in out

        code, out = self.run(
            "add", str(pickled), "m", "--allow-code", "--state-dir", state, capsys=capsys
        )
        assert code == 0 and "runs code when it loads" in out

    def test_verify_fails_when_a_file_no_longer_matches(
        self, tmp_path: Path, model: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state"
        artifact = ArtifactStore.in_state_dir(state).add(model, "imu")

        assert self.run("verify", "--state-dir", str(state), capsys=capsys)[0] == 0

        ArtifactStore.in_state_dir(state).path_of(artifact).write_bytes(b"tampered")
        code, out = self.run("verify", "--state-dir", str(state), capsys=capsys)
        assert code == 1 and "1 of 1" in out

    def test_a_bad_metadata_pair_is_refused(
        self, tmp_path: Path, model: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = self.run(
            "add", str(model), "imu", "--meta", "nonsense", "--state-dir", str(tmp_path),
            capsys=capsys,
        )  # fmt: skip

        assert code == 1 and "KEY=VALUE" in out

    def test_an_empty_store_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = self.run("list", "--state-dir", str(tmp_path), capsys=capsys)

        assert code == 0 and "none." in out
        assert str((tmp_path / STORE_DIRNAME).resolve()) in out, "which store, so a wrong one shows"

    def test_adding_names_the_store_it_went_to(
        self, tmp_path: Path, model: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state"
        state.mkdir()

        code, out = self.run("add", str(model), "imu", "--state-dir", str(state), capsys=capsys)

        assert code == 0 and str((state / STORE_DIRNAME).resolve()) in out
        assert "no state directory" not in out

    def test_adding_to_a_state_directory_that_was_not_there_warns(
        self, tmp_path: Path, model: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Usually a command run from another folder than Wactorz's own.
        code, out = self.run(
            "add", str(model), "imu", "--state-dir", str(tmp_path / "elsewhere"), capsys=capsys
        )

        assert code == 0
        assert "There was no state directory" in out and "WACTORZ_STATE_DIR" in out


def test_describe_is_json() -> None:
    artifact = artifacts_module.Artifact("m", 1, "0" * 64, "onnx", 3, False, 0.0, {"a": 1})

    assert json.loads(json.dumps(describe(artifact)))["pinned"] == f"m:1@{'0' * 64}"


def test_the_hashing_stream_counts_what_passes() -> None:
    out = io.BytesIO()
    hashing = artifacts_module._Hashing(out)

    hashing.write(b"abc")

    assert (hashing.size, out.getvalue()) == (3, b"abc")
    assert hashing.hexdigest() == hashlib.sha256(b"abc").hexdigest()


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        ((1 << 20) - 1, "1024.0 KB"),
        (1 << 20, "1.0 MB"),
        (5 << 20, "5.0 MB"),
        (3 << 40, "3072.0 GB"),
    ],
)
def test_a_size_is_shown_in_the_unit_that_fits(size: int, shown: str) -> None:
    assert artifacts_cli._size(size) == shown


def test_verify_can_check_one_version(
    tmp_path: Path, model: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ArtifactStore.in_state_dir(tmp_path).add(model, "imu")

    with caplog.at_level(logging.INFO, logger="wactorz.artifacts_cli"):
        code = artifacts_cli.main(["verify", "imu:1", "--state-dir", str(tmp_path)])

    assert code == 0 and "1 artifact(s) match" in caplog.text


def test_a_directory_is_guessed_as_one(tmp_path: Path) -> None:
    assert artifacts_module.guess_format(tmp_path) == "dir"


def test_an_index_that_does_not_parse_is_refused(store: ArtifactStore, model: Path) -> None:
    store.add(model, "imu")
    store.index_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ArtifactError, match="cannot be read"):
        store.names()


def test_a_store_that_stays_busy_is_refused_rather_than_waited_on_for_ever(
    store: ArtifactStore, model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts_module, "LOCK_WAIT_S", 0.0)
    store.add(model, "imu")
    held = StateLock(store.root)
    held.acquire()
    try:
        with pytest.raises(ArtifactError, match="stayed busy"):
            store.add(model, "imu")
    finally:
        held.release()

    assert [a.version for a in store.versions("imu")] == [1]
