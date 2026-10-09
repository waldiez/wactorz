"""Raw bytes kept with `persist_bytes`, so a model file can travel as state.

A migration ships an agent's state as JSON, and a node keeps it as JSON, so a
value of ``bytes`` is left behind. Stored as text it is a value like any other:
it is written on a node, read back after a restart, and goes with a migration.
"""

import json
from pathlib import Path

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core.state_snapshot import json_safe

#: Not valid UTF-8, so a round trip cannot pass by treating the bytes as text.
WEIGHTS = bytes(range(256)) * 3


def _agent(tmp_path: Path) -> DynamicAgent:
    return DynamicAgent(name="scorer", code="", persistence_dir=str(tmp_path))


class TestOnTheActor:
    def test_what_is_stored_comes_back_as_the_same_bytes(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)

        agent.persist_bytes("model", WEIGHTS)

        assert agent.recall_bytes("model") == WEIGHTS

    def test_what_is_stored_can_travel_as_json(self, tmp_path: Path) -> None:
        """The value a migration would ship is text, so nothing is dropped."""
        agent = _agent(tmp_path)
        agent.persist_bytes("model", WEIGHTS)

        safe, dropped = json_safe({"model": agent.recall("model")})

        assert dropped == []
        assert json.loads(json.dumps(safe)) == safe

    async def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        _agent(tmp_path).persist_bytes("model", WEIGHTS)

        again = _agent(tmp_path)
        await again._load_persistent_state()

        assert again.recall_bytes("model") == WEIGHTS

    def test_nothing_stored_reads_as_none(self, tmp_path: Path) -> None:
        assert _agent(tmp_path).recall_bytes("model") is None

    def test_a_key_holding_something_else_is_refused(self, tmp_path: Path) -> None:
        """Decoding a dict as base64 would fail somewhere less clear."""
        agent = _agent(tmp_path)
        agent.persist("model", {"mean": [0.0, 0.0, 1.0]})

        with pytest.raises(TypeError, match="model"):
            agent.recall_bytes("model")


class TestThroughTheAgentAPI:
    """What generated and catalogue code is handed as `agent`."""

    def test_the_round_trip_is_the_same(self, tmp_path: Path) -> None:
        api = AgentAPI(_agent(tmp_path))

        api.persist_bytes("model", WEIGHTS)

        assert api.recall_bytes("model") == WEIGHTS

    def test_nothing_stored_reads_as_none(self, tmp_path: Path) -> None:
        assert AgentAPI(_agent(tmp_path)).recall_bytes("model") is None
