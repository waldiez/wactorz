"""Tests for `_RemoteAgent` JSON state persistence (save/load round-trip)."""

import json

from wactorz.remote_runner import _RemoteAgent

SAMPLE = {"note": "café ☕ — δοκιμή", "n": 1}


def _make_agent(state_path):
    # Bypass __init__ (needs a live runner); exercise only the persistence methods.
    agent = _RemoteAgent.__new__(_RemoteAgent)
    agent.name = "state-test"
    agent._state_path = state_path
    agent._persistent_state = {}
    return agent


def test_save_load_round_trip(tmp_path):
    """State survives a save→load cycle unchanged."""
    path = tmp_path / "agent_state.json"
    saver = _make_agent(path)
    saver._persistent_state = dict(SAMPLE)
    saver._save_state()

    loader = _make_agent(path)
    loader._load_state()
    assert loader._persistent_state == SAMPLE


def test_load_state_reads_utf8_multibyte(tmp_path):
    """A state file with raw UTF-8 multibyte content loads correctly.

    Guards the explicit ``encoding="utf-8"`` on the read: without it, Windows
    would decode with the locale default (cp1252) and corrupt or raise.
    """
    path = tmp_path / "agent_state.json"
    # As ensure_ascii=False, a hand-edit, or a cross-version file would produce.
    path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    assert any(b > 127 for b in path.read_bytes())  # fixture really wrote multibyte

    agent = _make_agent(path)
    agent._load_state()
    assert agent._persistent_state == SAMPLE


def test_load_state_missing_file_is_noop(tmp_path):
    agent = _make_agent(tmp_path / "missing.json")
    agent._load_state()
    assert agent._persistent_state == {}
