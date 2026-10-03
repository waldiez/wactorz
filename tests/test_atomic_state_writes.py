"""An interrupted state save must not destroy the state it was replacing.

Writing in place truncates the file first, so a crash partway through left a
file that existed but could not be unpickled — and every reader treats an
unreadable state file as an absent one. The agent came back with an empty state
dict rather than a stale one, and nothing in the logs said why.

The legacy path in ``Actor`` is the one that matters most: ``persist()`` writes
the entire state dict on every call, so an interrupted write there loses every
key, not the one being written.
"""

import pickle
from pathlib import Path

import pytest

from wactorz.core.actor import Actor
from wactorz.core.atomic_io import write_pickle
from wactorz.core.persistence.pickle_store import PickleStore


class _Unpicklable:
    """Fails partway through being written, as a full disk or a crash would."""

    def __reduce__(self) -> None:
        raise RuntimeError("boom")


class TestWritePickle:
    def test_a_failed_write_leaves_the_previous_contents(self, tmp_path: Path) -> None:
        target = tmp_path / "state.pkl"
        write_pickle(target, {"generation": 1})

        with pytest.raises(RuntimeError):
            write_pickle(target, {"generation": 2, "bad": _Unpicklable()})

        assert pickle.loads(target.read_bytes()) == {"generation": 1}

    def test_a_failed_write_leaves_no_debris(self, tmp_path: Path) -> None:
        target = tmp_path / "state.pkl"
        write_pickle(target, {"generation": 1})

        with pytest.raises(RuntimeError):
            write_pickle(target, {"bad": _Unpicklable()})

        # A temp file left behind accumulates one per failure, forever.
        assert [p.name for p in tmp_path.iterdir()] == ["state.pkl"]

    def test_it_creates_a_file_that_was_not_there(self, tmp_path: Path) -> None:
        target = tmp_path / "state.pkl"
        write_pickle(target, {"generation": 1})
        assert pickle.loads(target.read_bytes()) == {"generation": 1}


def _on_disk(tmp_path: Path, agent: str) -> dict:
    """What a restart would read for ``agent``."""
    return pickle.loads((tmp_path / agent / "state.pkl").read_bytes())


class TestPickleStore:
    """A state that cannot be written costs that write, not the file.

    The store keeps the state in memory and writes the file after; with no
    event loop running, as here, it writes at once.
    """

    def test_a_failed_save_keeps_the_last_good_file(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.save("worker", {"generation": 1})

        store.save("worker", {"bad": _Unpicklable()})

        # Not {} — that is what the truncating write produced, and it is
        # indistinguishable from an agent that had never saved at all.
        assert _on_disk(tmp_path, "worker") == {"generation": 1}

    def test_a_lost_save_is_reported_with_the_file_it_was_for(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = PickleStore(str(tmp_path))

        store.save("worker", {"bad": _Unpicklable()})

        assert "state.pkl was not written" in caplog.text
        assert "worker" in caplog.text

    def test_the_agent_keeps_its_state_in_memory_all_the_same(self, tmp_path: Path) -> None:
        # It carries on with what it has; only a restart would miss it.
        store = PickleStore(str(tmp_path))
        bad = _Unpicklable()

        store.save("worker", {"bad": bad, "count": 3})

        assert store.load("worker") == {"bad": bad, "count": 3}

    def test_a_later_save_that_can_be_written_is(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.save("worker", {"bad": _Unpicklable()})

        store.save("worker", {"generation": 1})

        assert _on_disk(tmp_path, "worker") == {"generation": 1}


class TestLegacyActorPath:
    async def test_a_failed_persist_keeps_every_other_key(self, tmp_path: Path) -> None:
        class _Agent(Actor):
            async def handle_message(self, message):  # pragma: no cover
                return None

        agent = _Agent(name="worker")
        agent._persistence_api = None
        agent._persistence_dir = tmp_path
        agent.persist("keep_me", "value")

        # persist() rewrites the whole dict, so this failure used to take
        # keep_me with it.
        agent.persist("bad", _Unpicklable())

        agent._persistent_state = {}
        await agent._load_persistent_state()
        assert agent._persistent_state.get("keep_me") == "value"
