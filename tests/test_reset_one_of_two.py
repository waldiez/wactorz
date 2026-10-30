"""A reset of one of two agents of a name: that agent's goes, the other's stays.

A name is unique on a host, so what a host keeps by name is its own agent's;
of what main keeps for every host, the chat is told apart by the agent's id and
the metrics history by its node.
"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz import reset
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.persistence import WactorzDB
from wactorz.web import lifecycle

HERE_ID = "11111111-1111-4111-8111-111111111111"
PI_ID = "22222222-2222-4222-8222-222222222222"
HERE = {"name": "tally", "node": "", "actor_id": HERE_ID}
ON_PI = {"name": "tally", "node": "pi", "actor_id": PI_ID}


@pytest.fixture(name="path")
def path_fixture(tmp_path: Path) -> Path:
    """A database with both agents' turns, samples, kv and spawn entries."""
    path = tmp_path / "w.db"
    now = time.time()
    with WactorzDB(str(path)) as db:
        db.write_chat_log(now, "tally", "user", "to main", agent_id=HERE_ID)
        db.write_chat_log(now, "tally", "user", "to pi", agent_id=PI_ID)
        db.write_metrics_history(
            [
                {"ts": now, "agent": "tally", "node": ""},
                {"ts": now, "agent": "tally", "node": "pi"},
            ],
            [],
        )
        db.kv_set("tally", "conversation_history", ["main's own"])
        db.kv_set("tally", "_final_cost", 1.5)
        db.kv_set("main", "_spawned_agents_by_id", {HERE_ID: HERE, PI_ID: ON_PI})
    return path


def _turns(path: Path) -> list[str]:
    with WactorzDB(str(path)) as db:
        return [row["content"] for row in db.query_chat_log()]


def _samples(path: Path) -> list[str]:
    with WactorzDB(str(path)) as db:
        return [row["node"] or "" for row in db.query_agent_history("tally", 0)]


def _kv(path: Path, agent: str, key: str) -> Any:
    with WactorzDB(str(path)) as db:
        return db.kv_get(agent, key, None)


class TestTheChat:
    def test_the_one_on_a_node_goes_and_mains_stays(self, path: Path, tmp_path: Path) -> None:
        reset.reset_chat("tally", str(path), str(tmp_path), actor_id=PI_ID, on_main=False)

        assert _turns(path) == ["to main"]
        assert _kv(path, "tally", "conversation_history") == ["main's own"]

    def test_mains_goes_with_its_conversation_and_the_nodes_stays(
        self, path: Path, tmp_path: Path
    ) -> None:
        reset.reset_chat("tally", str(path), str(tmp_path), actor_id=HERE_ID)

        assert _turns(path) == ["to pi"]
        assert _kv(path, "tally", "conversation_history") is None

    def test_a_file_only_its_thread_named_goes_with_it(
        self, path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone: list[set[str]] = []
        monkeypatch.setattr(reset, "discard", lambda ids, _dir: gone.append(set(ids)) or 0)
        with WactorzDB(str(path)) as db:
            for agent_id, file_id in ((PI_ID, "a" * 32), (HERE_ID, "b" * 32)):
                db.write_chat_log(
                    1.0,
                    "tally",
                    "user",
                    "file",
                    attachments=[{"id": file_id, "name": "n", "mime": "m", "size": 1}],
                    agent_id=agent_id,
                )

        reset.reset_chat("tally", str(path), str(tmp_path), actor_id=PI_ID, on_main=False)

        assert gone == [{"a" * 32}]

    def test_with_no_id_every_agent_of_the_name_goes_as_before(
        self, path: Path, tmp_path: Path
    ) -> None:
        reset.reset_chat("tally", str(path), str(tmp_path))

        assert _turns(path) == []


class TestTheMetrics:
    def test_the_one_on_a_node_goes_and_mains_stays(self, path: Path) -> None:
        reset.reset_metrics("tally", str(path), node="pi", on_main=False)

        assert _samples(path) == [""]
        assert _kv(path, "tally", "_final_cost") == 1.5

    def test_mains_goes_and_the_nodes_stays(self, path: Path) -> None:
        reset.reset_metrics("tally", str(path), node="")

        assert _samples(path) == ["pi"]
        assert _kv(path, "tally", "_final_cost") is None


class TestTheSpawnRegistry:
    def test_only_that_entry_goes(self, path: Path) -> None:
        reset.reset_spawns("tally", str(path), actor_id=PI_ID)

        assert _kv(path, "main", "_spawned_agents_by_id") == {HERE_ID: HERE}

    def test_with_no_id_every_entry_of_the_name_goes(self, path: Path) -> None:
        reset.reset_spawns("tally", str(path))

        assert _kv(path, "main", "_spawned_agents_by_id") == {}


class _Main:
    """What withdrawing a spawn reaches on main, with both agents kept."""

    def __init__(self) -> None:
        self.registry = SpawnRegistry({HERE_ID: HERE, PI_ID: ON_PI})
        self.removed: list[str | None] = []
        self.nodes = SimpleNamespace(is_online=lambda _node: True)

    def _get_spawn_registry(self) -> Any:
        return self.registry

    def _remove_from_spawn_registry(self, _name: str, *, actor_id: str | None = None) -> None:
        self.removed.append(actor_id)

    async def _update_node_desired_state(self, _node: str) -> None:
        await asyncio.sleep(0)


class TestWithdrawingOne:
    async def test_only_that_agent_is_taken_from_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main()
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _registry: main)

        await lifecycle.purge_spawn_reconcile("tally", PI_ID)

        assert main.removed == [PI_ID]
