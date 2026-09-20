"""A node agent's state is JSON on disk, and survives the round trip.

JSON rather than the pickle main keeps, because this file has a second reader:
a migration ships it over MQTT to whichever machine the agent moves to, and the
two need not be running the same Python.
"""

import json

from wactorz.node.state import JsonState, json_safe, state_path

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


class TestWhatCanTravel:
    def test_json_values_survive_and_the_rest_are_named(self):
        kept, dropped = json_safe({"count": 3, "capture": object()})

        assert kept == {"count": 3}
        assert dropped == ["capture"]
