"""A node agent's state is JSON on disk, and survives the round trip.

JSON rather than the pickle main keeps, because this file has a second reader:
a migration ships it over MQTT to whichever machine the agent moves to, and the
two need not be running the same Python.
"""

import json

import pytest

from tests.waiting import quiet
from wactorz.core import deferred_write
from wactorz.core.state_snapshot import json_safe
from wactorz.node import state as node_state
from wactorz.node.state import JsonState, state_path

#: The delay these tests give the node's writer, in place of the real one.
DELAY_S = 0.1

SAMPLE = {"note": "café ☕ — δοκιμή", "n": 1}


def _state(path) -> JsonState:
    return JsonState(path, "state-test")


def test_save_load_round_trip(tmp_path):
    """State survives a save→load cycle unchanged."""
    path = tmp_path / "agent_state.json"
    _state(path).save(dict(SAMPLE))

    assert _state(path).load() == SAMPLE


def test_load_state_reads_utf8_multibyte(tmp_path):
    """A state file with raw UTF-8 multibyte content loads correctly.

    Guards the explicit ``encoding="utf-8"`` on the read: without it, Windows
    would decode with the locale default (cp1252) and corrupt or raise.
    """
    path = tmp_path / "agent_state.json"
    # As ensure_ascii=False, a hand-edit, or a cross-version file would produce.
    path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    assert any(b > 127 for b in path.read_bytes())  # fixture really wrote multibyte

    assert _state(path).load() == SAMPLE


def test_load_state_missing_file_is_noop(tmp_path):
    assert _state(tmp_path / "missing.json").load() == {}


def test_delete_removes_the_file(tmp_path):
    """Without this a deleted agent's memory returns on the next runner start."""
    path = tmp_path / "agent_state.json"
    _state(path).save(dict(SAMPLE))

    assert _state(path).delete() is True
    assert not path.exists()
    assert _state(path).delete() is False


def test_a_separator_in_the_name_stays_in_the_state_directory(tmp_path):
    """A name is flattened, not nested — otherwise it could walk out of the dir."""
    path = state_path(tmp_path, "../escape/agent")

    assert path.parent == tmp_path


class TestOneBadValueDoesNotCostTheRest:
    """A save that cannot serialise everything must not lose what it can.

    Streaming into an opened file truncated it first and stopped at the first
    value that would not go, leaving invalid JSON where a good file had been —
    which the next load quarantines, so the agent starts empty. One
    `agent.persist('when', datetime.now())` cost it every other key it held.
    """

    def test_the_serialisable_keys_are_still_written(self, tmp_path):
        state = _state(tmp_path / "agent_state.json")

        state.save({"calibration": 1.5, "readings": [1, 2], "capture": object()})

        assert state.load() == {"calibration": 1.5, "readings": [1, 2]}

    def test_an_earlier_good_state_survives_it(self, tmp_path):
        path = tmp_path / "agent_state.json"
        _state(path).save({"calibration": 1.5, "readings": [1, 2]})

        _state(path).save({"capture": object()})

        # Nothing of this save could be written, and what was there is intact
        # rather than replaced by half a file.
        assert json.loads(path.read_text(encoding="utf-8")) == {}
        assert _state(path).load() == {}

    def test_it_says_which_key_it_dropped(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            _state(tmp_path / "agent_state.json").save({"ok": 1, "capture": object()})

        assert "capture" in caplog.text
        assert "ok" not in caplog.text.split("Not persisting")[1].split(":")[0]

    def test_the_file_is_replaced_in_one_step(self, tmp_path):
        # Through `write_text`, so a board that loses power mid-save has either
        # the old contents or the new ones, never half of either.
        path = tmp_path / "agent_state.json"
        _state(path).save({"n": 1})

        _state(path).save({"n": 2})

        assert list(tmp_path.glob("*.tmp")) == []
        assert _state(path).load() == {"n": 2}


class TestWritingOnlyWhenThereIsSomethingToWrite:
    """Every key is saved by rewriting the whole file.

    Agents persist a value on every tick that changes far less often —
    `agent.persist("plugs", agent.state["plugs"])` — and that rewrote and
    fsynced the lot each time. On a Raspberry Pi 5's SD card the write is
    ~4.3ms against ~0.07ms for noticing there is nothing to do.
    """

    def test_saving_the_same_state_again_does_not_rewrite_it(self, tmp_path):
        path = tmp_path / "agent_state.json"
        state = _state(path)
        state.save({"plugs": {"a": True}})
        before = path.stat().st_mtime_ns

        state.save({"plugs": {"a": True}})

        assert path.stat().st_mtime_ns == before

    def test_a_write_that_failed_is_tried_again_by_the_next_save(self, tmp_path, monkeypatch):
        # The state is marked as written when the write lands, not when it is
        # asked for. Marked early, a disk that was full for one write would
        # leave the file stale for as long as the state stayed the same.
        path = tmp_path / "agent_state.json"
        state = _state(path)
        state.save({"n": 1})
        real = deferred_write.write_bytes

        def _full(target, data):
            raise OSError("No space left on device")

        monkeypatch.setattr(deferred_write, "write_bytes", _full)
        state.save({"n": 2})
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}

        monkeypatch.setattr(deferred_write, "write_bytes", real)
        state.save({"n": 2})

        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 2}

    def test_a_change_is_written(self, tmp_path):
        path = tmp_path / "agent_state.json"
        state = _state(path)
        state.save({"plugs": {"a": True}})

        state.save({"plugs": {"a": False}})

        assert state.load() == {"plugs": {"a": False}}

    def test_a_file_that_went_missing_is_written_again(self, tmp_path):
        # The skip rests on the file saying what we last wrote. If something
        # removed it, it does not.
        path = tmp_path / "agent_state.json"
        state = _state(path)
        state.save({"n": 1})
        path.unlink()

        state.save({"n": 1})

        assert state.load() == {"n": 1}

    def test_a_fresh_reader_writes_even_if_the_content_matches(self, tmp_path):
        # A new JsonState has written nothing yet, so it cannot assume the file
        # holds what it is about to save.
        path = tmp_path / "agent_state.json"
        _state(path).save({"n": 1})
        path.write_text("{}", encoding="utf-8")

        _state(path).save({"n": 1})

        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}


class TestSayingWhenStateHasGrownExpensive:
    def test_it_warns_once_past_the_threshold(self, tmp_path, caplog):
        state = _state(tmp_path / "agent_state.json")
        big = {"history": ["x" * 1000] * 700}

        with caplog.at_level("WARNING"):
            state.save(big)
            state.save({**big, "n": 1})

        assert caplog.text.count("Every persist rewrites all of it") == 1

    def test_ordinary_state_says_nothing(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            _state(tmp_path / "agent_state.json").save({"readings": [1, 2, 3]})

        assert "rewrites all of it" not in caplog.text


class TestWhatCanTravel:
    def test_json_values_survive_and_the_rest_are_named(self):
        kept, dropped = json_safe({"count": 3, "capture": object()})

        assert kept == {"count": 3}
        assert dropped == ["capture"]


class TestWritingOffTheEventLoop:
    """Under a running loop the file follows a moment after the save.

    A node is the machine with the slow storage, and an agent there saves a
    value on every tick. Forcing each save to disk on the event loop held every
    other agent on the node for as long as the disk took.
    """

    @pytest.fixture(autouse=True)
    def _short_delay(self, monkeypatch):
        monkeypatch.setattr(node_state._WRITER, "_delay", DELAY_S)

    async def test_a_save_returns_before_the_file_is_written(self, tmp_path):
        path = tmp_path / "agent_state.json"

        _state(path).save({"n": 1})

        assert not path.exists()
        await quiet(node_state._WRITER)
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}

    async def test_a_value_saved_every_tick_is_written_once_as_it_ended(
        self, tmp_path, monkeypatch
    ):
        written = []
        real = deferred_write.write_bytes

        def _counting(target, data):
            written.append(target)
            real(target, data)

        monkeypatch.setattr(deferred_write, "write_bytes", _counting)
        path = tmp_path / "agent_state.json"
        state, values = _state(path), {"n": 0}

        for tick in range(200):
            values["n"] = tick
            state.save(values)
        await quiet(node_state._WRITER)

        assert written == [path]
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 199}

    async def test_an_agent_started_again_reads_what_the_last_one_saved(self, tmp_path):
        # A supervisor restart builds a new agent under the same name straight
        # away, before the one it replaces has had its last save written.
        path = tmp_path / "agent_state.json"
        _state(path).save({"n": 7})

        assert _state(path).load() == {"n": 7}

    async def test_a_deleted_state_does_not_come_back(self, tmp_path):
        path = tmp_path / "agent_state.json"
        state = _state(path)
        state.save({"n": 1})

        state.delete()
        await quiet(node_state._WRITER)

        assert not path.exists()

    async def test_flushing_writes_what_is_waiting(self, tmp_path):
        # What a node does as it shuts down, and what an agent's stop does.
        path = tmp_path / "agent_state.json"
        _state(path).save({"n": 1})

        node_state.flush_states()

        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}
