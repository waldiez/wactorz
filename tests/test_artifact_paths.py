"""An agent opens the artifacts its spawn names, on main and on a node alike.

The spawn names models by reference; whoever spawns the agent pins them and
works out where each is on that host. Generated code asks for one by the key
its spawn used and gets a path: the file, or for a directory artifact the
directory it unpacks to, unpacked once and only ever inside where it belongs.
"""

import asyncio
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_node_runner import RecordingRunner
from tests.test_spawning import FakeRegistry, MainHost
from tests.waiting import until
from wactorz import artifacts_cli
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.mixins import spawning
from wactorz.agents.planner.validation import SYNC_METHODS
from wactorz.core.artifacts import (
    UNPACKED_DIRNAME,
    ArtifactError,
    ArtifactStore,
    pinned,
    unpack_directory,
)
from wactorz.state_archive import export_state, import_state

MODEL = b"onnx bytes"


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main's state directory, where a local spawn looks for the store."""
    state = tmp_path / "state"
    monkeypatch.setattr(spawning, "resolve_state_dir", lambda: str(state))
    return state


@pytest.fixture(name="store")
def store_fixture(state: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(state)


def a_model(tmp_path: Path) -> Path:
    path = tmp_path / "detector.onnx"
    path.write_bytes(MODEL)
    return path


def a_directory(tmp_path: Path) -> Path:
    root = tmp_path / "tokenizer"
    (root / "vocab").mkdir(parents=True)
    (root / "config.json").write_text('{"size": 3}', encoding="utf-8")
    (root / "vocab" / "words.txt").write_text("a\nb\nc\n", encoding="utf-8")
    return root


def a_tar(path: Path, *members: tuple[tarfile.TarInfo, bytes | None]) -> Path:
    with tarfile.open(path, "w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def member(
    name: str, kind: bytes = tarfile.REGTYPE, data: bytes = b"x"
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data


# ── Unpacking ─────────────────────────────────────────────────────────────────


class TestUnpacking:
    def test_a_directory_comes_back_as_it_was_stored(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))

        unpacked = store.readable_path(entry)

        assert unpacked == store.unpacked / entry["sha256"]
        assert (unpacked / "config.json").read_text(encoding="utf-8") == '{"size": 3}'
        assert (unpacked / "vocab" / "words.txt").read_text(encoding="utf-8") == "a\nb\nc\n"

    def test_a_file_is_read_where_it_is_kept(self, store: ArtifactStore, tmp_path: Path) -> None:
        entry = pinned(store.add(a_model(tmp_path), "imu"))

        assert store.readable_path(entry) == store.path_of(store.resolve("imu"))

    def test_it_is_unpacked_once(self, store: ArtifactStore, tmp_path: Path) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))
        first = store.readable_path(entry)
        marker = first / "added-later"
        marker.write_text("kept", encoding="utf-8")

        assert store.readable_path(entry) == first
        assert marker.exists(), "a directory that is there is not unpacked again"

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("../outside", tarfile.REGTYPE),
            ("/etc/passwd", tarfile.REGTYPE),
            ("inside/../../outside", tarfile.REGTYPE),
            ("link", tarfile.SYMTYPE),
            ("hard", tarfile.LNKTYPE),
            ("device", tarfile.CHRTYPE),
            ("fifo", tarfile.FIFOTYPE),
        ],
    )
    def test_what_would_land_outside_or_is_not_a_file_is_refused(
        self, tmp_path: Path, name: str, kind: bytes
    ) -> None:
        archive = a_tar(tmp_path / "bad.tar", member("ok.txt"), member(name, kind))
        target = tmp_path / "unpacked" / "abc"

        with pytest.raises(ArtifactError):
            unpack_directory(archive, target)

        assert not target.exists()
        assert not (tmp_path / "outside").exists()
        assert list((tmp_path / "unpacked").iterdir()) == [], "nothing half unpacked is left"

    def test_removing_the_artifact_removes_what_it_unpacked_to(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))
        unpacked = store.readable_path(entry)

        store.remove("tokenizer:1")

        assert not unpacked.exists()

    def test_an_export_leaves_it_out_and_it_is_unpacked_again_after(
        self, state: Path, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))
        store.readable_path(entry)

        export_state(tmp_path / "state.tar", state)
        with tarfile.open(tmp_path / "state.tar") as tar:
            assert not [n for n in tar.getnames() if UNPACKED_DIRNAME in n]
        import_state(tmp_path / "state.tar", tmp_path / "restored")

        restored = ArtifactStore.in_state_dir(tmp_path / "restored")
        assert (restored.readable_path(entry) / "config.json").exists()


# ── What generated code is handed ─────────────────────────────────────────────


def an_agent(tmp_path: Path, artifacts: dict[str, Any]) -> DynamicAgent:
    return DynamicAgent(code="", name="reader", persistence_dir=str(tmp_path), artifacts=artifacts)


class TestTheAgentsView:
    def test_it_asks_by_the_key_its_spawn_used(self, tmp_path: Path) -> None:
        agent = an_agent(tmp_path, {"model": {"name": "imu", "version": 1, "path": "/x/y"}})

        assert agent._api.artifact_path("model") == "/x/y"
        assert agent._api.artifacts == {"model": {"name": "imu", "version": 1, "path": "/x/y"}}

    def test_a_key_it_was_not_given_says_which_it_was(self, tmp_path: Path) -> None:
        agent = an_agent(tmp_path, {"model": {"path": "/x"}, "labels": {"path": "/y"}})

        with pytest.raises(KeyError, match="labels, model"):
            agent._api.artifact_path("weights")

    def test_an_agent_given_none_has_none(self, tmp_path: Path) -> None:
        agent = an_agent(tmp_path, {})

        assert agent._api.artifacts == {}
        with pytest.raises(KeyError, match="none"):
            agent._api.artifact_path("model")

    def test_what_it_is_handed_is_its_own_copy(self, tmp_path: Path) -> None:
        agent = an_agent(tmp_path, {"model": {"path": "/x"}})

        agent._api.artifacts["model"]["path"] = "/elsewhere"

        assert agent._api.artifact_path("model") == "/x"


# ── Spawned on main ───────────────────────────────────────────────────────────


class TestSpawnedOnMain:
    async def test_it_is_handed_the_path_of_each(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        artifact = store.add(a_model(tmp_path), "imu")
        host = MainHost(FakeRegistry())

        await host._spawn_local_from_config(
            {"name": "reader", "code": "x = 1", "artifacts": {"model": "imu"}}
        )

        _, kwargs = host.spawn_calls[-1]
        assert kwargs["artifacts"]["model"]["path"] == str(store.path_of(artifact))
        assert kwargs["artifacts"]["model"]["version"] == 1

    async def test_what_is_saved_names_the_pinned_version(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        artifact = store.add(a_model(tmp_path), "imu")
        host = MainHost(FakeRegistry())
        config = {"name": "reader", "code": "x = 1", "artifacts": {"model": "imu"}}

        await host._spawn_local_from_config(config)

        assert host.registered[-1]["artifacts"] == {
            "model": {**pinned(artifact), "follows": "active"}
        }
        assert config["artifacts"] == {"model": "imu"}, "the caller's config is left alone"

    async def test_a_directory_is_handed_unpacked(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        store.add(a_directory(tmp_path), "tokenizer")
        host = MainHost(FakeRegistry())

        await host._spawn_local_from_config(
            {"name": "reader", "code": "x = 1", "artifacts": {"tokens": "tokenizer"}}
        )

        _, kwargs = host.spawn_calls[-1]
        assert (Path(kwargs["artifacts"]["tokens"]["path"]) / "config.json").exists()

    async def test_one_that_is_not_stored_is_not_spawned(
        self, store: ArtifactStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        host = MainHost(FakeRegistry())

        actor = await host._spawn_local_from_config(
            {"name": "reader", "code": "x = 1", "artifacts": {"model": "missing:1"}}
        )

        assert actor is None
        assert host.spawn_calls == []
        assert "missing:1" in caplog.text

    async def test_a_pinned_version_since_removed_is_not_spawned(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # A record pinned before must still name what is stored, or the agent
        # would start on a model it never ran.
        entry = pinned(store.add(a_model(tmp_path), "imu"))
        store.remove("imu:1")
        host = MainHost(FakeRegistry())

        actor = await host._spawn_local_from_config(
            {"name": "reader", "code": "x = 1", "artifacts": {"model": entry}}
        )

        assert actor is None


# ── Spawned on a node ─────────────────────────────────────────────────────────


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    runner = RecordingRunner(tmp_path / "node-state")
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


def cached_on(runner: RecordingRunner, store: ArtifactStore, entry: dict[str, Any]) -> None:
    """Put a stored artifact in the node's cache, as an earlier fetch would have."""
    runner.artifacts.directory.mkdir(parents=True, exist_ok=True)
    runner.artifacts.path(entry["sha256"]).write_bytes(
        store.path_of(store.resolve(f"{entry['name']}:{entry['version']}")).read_bytes()
    )


#: A program that reads its model when it starts and says what it found.
READER = """
async def setup(agent):
    path = agent.artifact_path("model")
    agent.persist("read", open(path, "rb").read().decode())
"""

#: The same, for a directory.
LISTER = """
import os

async def setup(agent):
    path = agent.artifact_path("tokens")
    agent.persist("files", sorted(os.listdir(path)))
"""


class TestSpawnedOnANode:
    async def test_its_program_reads_the_model(
        self, runner: RecordingRunner, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_model(tmp_path), "imu"))
        cached_on(runner, store, entry)

        await runner.spawn_agent({"name": "reader", "code": READER, "artifacts": {"model": entry}})

        agent = runner.get("reader")
        assert agent is not None
        await until(lambda: agent.recall("read"), "the program to read its model")
        assert agent.recall("read") == MODEL.decode()

    async def test_its_program_reads_a_directory(
        self, runner: RecordingRunner, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))
        cached_on(runner, store, entry)

        await runner.spawn_agent({"name": "lister", "code": LISTER, "artifacts": {"tokens": entry}})

        agent = runner.get("lister")
        assert agent is not None
        await until(lambda: agent.recall("files"), "the program to list its directory")
        assert agent.recall("files") == ["config.json", "vocab"]
        assert (runner.artifacts.unpacked / entry["sha256"]).is_dir()

    async def test_a_directory_that_will_not_unpack_is_refused(
        self, runner: RecordingRunner, store: ArtifactStore, tmp_path: Path
    ) -> None:
        bad = a_tar(tmp_path / "bad.tar", member("../outside"))
        data = bad.read_bytes()
        entry = {
            "name": "evil",
            "version": 1,
            "sha256": hashlib.sha256(data).hexdigest(),
            "format": "dir",
            "size": len(data),
            "runs_code": False,
        }
        runner.artifacts.directory.mkdir(parents=True, exist_ok=True)
        runner.artifacts.path(entry["sha256"]).write_bytes(data)

        await runner.spawn_agent(
            {
                "name": "lister",
                "code": LISTER,
                "artifacts": {"tokens": entry},
                "_migration_token": "t",
            }
        )

        assert runner.get("lister") is None
        (answer,) = [p for t, p, _ in runner.published if t.endswith("/spawn_ack")]
        assert "could not be unpacked" in answer["refused"]

    async def test_trimming_takes_what_it_unpacked_to_as_well(
        self, runner: RecordingRunner, store: ArtifactStore, tmp_path: Path
    ) -> None:
        entry = pinned(store.add(a_directory(tmp_path), "tokenizer"))
        cached_on(runner, store, entry)
        unpacked = runner.artifacts.readable_path(entry)
        runner.artifacts.max_bytes = 1

        removed = await asyncio.to_thread(runner.artifacts.trim, set())

        assert removed == [entry["sha256"]]
        assert not unpacked.exists()


# ── The command ───────────────────────────────────────────────────────────────


def test_refusing_a_pickle_names_the_file_and_the_way_past(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pickled = tmp_path / "imu_model.pkl"
    pickled.write_bytes(b"x")

    with caplog.at_level("ERROR", logger="wactorz.artifacts_cli"):
        code = artifacts_cli.main(["add", str(pickled), "m", "--state-dir", str(tmp_path)])

    assert code == 1
    assert "imu_model.pkl is a pickle" in caplog.text
    assert "--allow-code" in caplog.text


def test_the_agent_api_lists_artifact_path_as_synchronous() -> None:
    # The planner strips `await` from these; awaiting a str is a runtime error.
    assert "artifact_path" in SYNC_METHODS
