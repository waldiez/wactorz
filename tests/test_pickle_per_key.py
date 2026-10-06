"""One value that no longer unpickles costs its own key, not the state beside it.

A model object pickled under one version of a library may not load under the
next, and a class an agent stored may be renamed. Each value in a state file is
pickled on its own, so such a value is left out when the file is read and the
counters and settings stored with it come back. Its bytes are kept and written
back unchanged, so the key returns once the code that reads it does.
"""

import pickle
import sys
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.actor import Actor
from wactorz.core.deferred_write import LARGE_STATE_BYTES
from wactorz.core.persistence import PersistenceAPI, WactorzDB
from wactorz.core.persistence.pickle_store import (
    NotAStateFileError,
    PickleStore,
    decode_state,
    encode_state,
    read_state_file,
)

AGENT = "detector"


class Model:
    """Stands in for a library object; deleting the class makes it unloadable."""

    def __init__(self, weights: list[int]) -> None:
        self.weights = weights

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Model) and other.weights == self.weights

    __hash__ = None  # pyright: ignore[reportAssignmentType]


def _state_file(base: Path) -> Path:
    return base / AGENT / "state.pkl"


def _restarted(base: Path) -> PickleStore:
    """A store over ``base`` with nothing held from before, as after a restart."""
    return PickleStore(str(base))


def _written(base: Path, state: dict[str, Any]) -> None:
    """Write ``state`` for the agent the way a running store would, then let it go."""
    store = PickleStore(str(base))
    store.save(AGENT, state)
    store.flush()


class TestReadingAFileWithOneBadValue:
    def test_the_other_keys_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _written(tmp_path, {"model": Model([1, 2]), "count": 41, "threshold": 0.7})
        monkeypatch.delattr(sys.modules[__name__], "Model")

        assert _restarted(tmp_path).load(AGENT) == {"count": 41, "threshold": 0.7}

    def test_the_file_is_not_moved_aside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _written(tmp_path, {"model": Model([1]), "count": 1})
        monkeypatch.delattr(sys.modules[__name__], "Model")

        _restarted(tmp_path).load(AGENT)

        assert _state_file(tmp_path).exists()
        assert not list(_state_file(tmp_path).parent.glob("*.corrupt.*"))

    def test_the_key_and_the_reason_are_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _written(tmp_path, {"model": Model([1]), "count": 1})
        monkeypatch.delattr(sys.modules[__name__], "Model")

        _restarted(tmp_path).load(AGENT)

        assert f"'{AGENT}' starts without model" in caplog.text
        assert "AttributeError" in caplog.text


class TestTheBadValueIsKept:
    def test_a_write_of_another_key_carries_it_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _written(tmp_path, {"model": Model([1, 2]), "count": 41})
        with monkeypatch.context() as gone:
            gone.delattr(sys.modules[__name__], "Model")
            store = _restarted(tmp_path)
            store.update(AGENT, "count", 42)
            store.flush()
            del store

        # The library is back: the model returns, with the newer count.
        assert _restarted(tmp_path).load(AGENT) == {"model": Model([1, 2]), "count": 42}

    def test_persisting_the_key_again_replaces_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _written(tmp_path, {"model": Model([1, 2])})
        with monkeypatch.context() as gone:
            gone.delattr(sys.modules[__name__], "Model")
            store = _restarted(tmp_path)
            store.update(AGENT, "model", "retrained")
            store.flush()
            del store

        assert _restarted(tmp_path).load(AGENT) == {"model": "retrained"}

    @pytest.mark.parametrize("how", ["remove", "save", "delete"])
    def test_removing_or_replacing_the_state_lets_it_go(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
    ) -> None:
        _written(tmp_path, {"model": Model([1]), "count": 1})
        with monkeypatch.context() as gone:
            gone.delattr(sys.modules[__name__], "Model")
            store = _restarted(tmp_path)
            if how == "remove":
                store.remove(AGENT, "model")
            elif how == "save":
                store.save(AGENT, {"count": 2})
            else:
                store.delete(AGENT)
                store.update(AGENT, "count", 2)
            store.flush()
            del store

        assert "model" not in _restarted(tmp_path).load(AGENT)


class TestTheFileFormat:
    def test_a_plain_dict_from_an_earlier_release_is_read(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(pickle.dumps({"count": 3, "model": Model([1])}))

        assert _restarted(tmp_path).load(AGENT) == {"count": 3, "model": Model([1])}

    def test_it_is_written_the_new_way_on_its_next_save(self, tmp_path: Path) -> None:
        path = _state_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(pickle.dumps({"count": 3}))
        store = _restarted(tmp_path)

        store.update(AGENT, "count", 4)
        store.flush()

        assert set(pickle.loads(path.read_bytes())) == {"wactorz_state_format", "values"}
        assert read_state_file(path).values == {"count": 4}

    def test_a_value_that_will_not_pickle_is_named_and_left_out(self) -> None:
        data, unpicklable = encode_state({"count": 1, "handle": lambda: None})

        assert unpicklable == ["handle"]
        assert decode_state(data).values == {"count": 1}

    def test_a_file_that_is_not_a_state_raises(self) -> None:
        with pytest.raises(NotAStateFileError):
            decode_state(pickle.dumps(["not", "a", "dict"]))


class TestThroughTheAgentsApi:
    def test_all_returns_what_could_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _written(tmp_path, {"model": Model([1]), "count": 41})
        monkeypatch.delattr(sys.modules[__name__], "Model")
        db = WactorzDB(str(tmp_path / "wactorz.db"))
        try:
            api = PersistenceAPI(db, _restarted(tmp_path), AGENT)

            assert api.all() == {"count": 41}
            assert api.get("model", "untrained") == "untrained"
        finally:
            db.close()


class TestRecallingAStoredNone:
    def test_it_reads_as_the_default(self, tmp_path: Path) -> None:
        # So `recall(key, [])` can be appended to without a None check.
        db = WactorzDB(str(tmp_path / "wactorz.db"))
        try:
            agent = _Agent(name=AGENT, persistence_dir=str(tmp_path))
            agent._persistence_api = PersistenceAPI(db, PickleStore(str(tmp_path)), AGENT)
            agent.persist("items", None)

            assert agent.recall("items", []) == []
        finally:
            db.close()


class _Agent(Actor):
    async def handle_message(self, message: Any) -> None:  # pragma: no cover - never sent one
        return None


class TestAnActorWithoutAStore:
    async def test_it_reads_the_file_a_value_at_a_time_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _Agent(name=AGENT, persistence_dir=str(tmp_path))
        agent.persist("model", Model([1]))
        agent.persist("count", 41)
        monkeypatch.delattr(sys.modules[__name__], "Model")

        again = _Agent(name=AGENT, persistence_dir=str(tmp_path))
        await again._load_persistent_state()

        assert again.recall("count") == 41
        assert again.recall("model") is None

    async def test_its_next_write_keeps_the_value_it_could_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _Agent(name=AGENT, persistence_dir=str(tmp_path))
        agent.persist("model", Model([1]))
        with monkeypatch.context() as gone:
            gone.delattr(sys.modules[__name__], "Model")
            again = _Agent(name=AGENT, persistence_dir=str(tmp_path))
            await again._load_persistent_state()
            again.persist("count", 1)

        later = _Agent(name=AGENT, persistence_dir=str(tmp_path))
        await later._load_persistent_state()
        assert later.recall("model") == Model([1])


class TestALargeState:
    def test_it_is_named_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        store = PickleStore(str(tmp_path))

        for tick in range(3):
            store.save(AGENT, {"history": "x" * LARGE_STATE_BYTES, "tick": tick})

        assert caplog.text.count(f"'{AGENT}' persists") == 1

    def test_a_small_one_says_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        PickleStore(str(tmp_path)).save(AGENT, {"history": "x" * 1000})

        assert "persists" not in caplog.text
