"""An unreadable state file must survive being unreadable.

Every load path treated a corrupt file as absent: log it, start with empty state,
carry on. That reads like graceful degradation, but the agent's next `persist`
writes over the file it could not read — so the only copy of whatever was in
there is destroyed moments after the one log line about it scrolls past. The
agent then looks healthy while having quietly lost everything it remembered.

The file is now moved aside first, so the next save cannot reach it and there is
something left to look at.
"""

import json
import pickle
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import Actor
from wactorz.core.atomic_io import quarantine_unreadable
from wactorz.core.persistence.pickle_store import PickleStore
from wactorz.remote_runner import _RemoteAgent


def _corrupt(path: Path) -> None:
    """Bytes that are not a pickle and not JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x80\x04 this is not a pickle")


def _quarantined(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.corrupt.*"))


class TestQuarantineUnreadable:
    def test_it_moves_the_file_aside_and_says_where(self, tmp_path: Path) -> None:
        path = tmp_path / "state.pkl"
        path.write_bytes(b"junk")

        moved = quarantine_unreadable(path)

        assert moved is not None
        assert not path.exists()
        assert moved.read_bytes() == b"junk"

    def test_a_second_failure_does_not_overwrite_the_first(self, tmp_path: Path) -> None:
        # Two bad starts must leave two files, or the earlier evidence is lost
        # to the very thing this prevents.
        first = tmp_path / "state.pkl"
        first.write_bytes(b"one")
        quarantine_unreadable(first)
        first.write_bytes(b"two")
        quarantine_unreadable(first)

        assert len(_quarantined(tmp_path)) == 2

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert quarantine_unreadable(tmp_path / "nope.pkl") is None


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> PickleStore:
    return PickleStore(str(tmp_path))


class TestPickleStoreLoad:
    def test_a_corrupt_file_still_reads_as_empty(self, store: PickleStore, tmp_path: Path) -> None:
        # The agent must still start — this is the part that was already right.
        _corrupt(tmp_path / "worker" / "state.pkl")

        assert store.load("worker") == {}

    def test_the_corrupt_file_survives_the_next_save(
        self, store: PickleStore, tmp_path: Path
    ) -> None:
        _corrupt(tmp_path / "worker" / "state.pkl")

        store.load("worker")
        store.save("worker", {"fresh": True})

        assert _quarantined(tmp_path / "worker"), "nothing was kept"
        assert store.load("worker") == {"fresh": True}

    def test_a_readable_file_is_left_alone(self, store: PickleStore, tmp_path: Path) -> None:
        store.save("worker", {"a": 1})

        assert store.load("worker") == {"a": 1}
        assert not _quarantined(tmp_path / "worker")


class TestRemoteRunnerLoadState:
    def test_a_corrupt_file_is_kept_rather_than_silently_dropped(self, tmp_path: Path) -> None:
        # This path swallowed the exception entirely — no log at all — so a
        # remote agent could lose its state with nothing recorded anywhere.
        agent = _RemoteAgent.__new__(_RemoteAgent)
        agent.name = "worker"
        agent._state_path = str(tmp_path / "state.json")
        agent._persistent_state = {}
        (tmp_path / "state.json").write_text("{not json", encoding="utf-8")

        agent._load_state()

        assert not agent._persistent_state
        assert _quarantined(tmp_path), "nothing was kept"

    def test_a_readable_file_loads_and_is_left_alone(self, tmp_path: Path) -> None:
        agent = _RemoteAgent.__new__(_RemoteAgent)
        agent.name = "worker"
        agent._state_path = str(tmp_path / "state.json")
        agent._persistent_state = {}
        (tmp_path / "state.json").write_text(json.dumps({"a": 1}), encoding="utf-8")

        agent._load_state()

        assert agent._persistent_state == {"a": 1}
        assert not _quarantined(tmp_path)


class _LoadableActor(Actor):
    """Concrete enough to construct; only the load path is exercised."""

    async def handle_message(self, message: Any) -> None:
        return None


def _bare_actor(state_dir: Path, persistence_api: Any) -> Any:
    actor: Any = _LoadableActor.__new__(_LoadableActor)
    actor.name = "worker"
    actor._persistence_dir = state_dir
    actor._persistent_state = {}
    actor._persistence_api = persistence_api
    return actor


class TestActorLegacyLoad:
    """Both branches read the same legacy pickle, and both dropped it."""

    @pytest.mark.parametrize("persistence_api", [None, object()])
    async def test_a_corrupt_legacy_pickle_is_kept(
        self, tmp_path: Path, persistence_api: Any
    ) -> None:
        actor = _bare_actor(tmp_path, persistence_api)
        _corrupt(tmp_path / "state.pkl")

        await actor._load_persistent_state()

        assert not actor._persistent_state
        assert _quarantined(tmp_path), "nothing was kept"

    async def test_a_readable_legacy_pickle_still_loads(self, tmp_path: Path) -> None:
        actor = _bare_actor(tmp_path, None)
        (tmp_path / "state.pkl").write_bytes(pickle.dumps({"a": 1}))

        await actor._load_persistent_state()

        assert actor._persistent_state == {"a": 1}
        assert not _quarantined(tmp_path)
