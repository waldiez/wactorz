"""A date, a set or a tuple in an agent's state goes wherever the agent goes.

A node keeps an agent's state as JSON, and a migration carries it as JSON, in
both directions. Each leg is checked here with the same values, so a value that
survives one and not another shows up as the leg that lost it.
"""

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.test_node_runner import CODE, RecordingRunner
from tests.test_spawning import FakeRegistry, MainHost
from wactorz.agents.mixins import spawning
from wactorz.core.blob_transfer import cannot_travel, chunks, json_part, pack
from wactorz.core.persistence import PickleStore, WactorzDB
from wactorz.core.state_codec import decode_state
from wactorz.core.state_snapshot import json_safe, why_it_cannot_travel
from wactorz.node.state import JsonState

#: What an agent might remember, in each kind JSON cannot hold on its own.
VALUES: dict[str, Any] = {
    "last_seen": dt.datetime(2026, 10, 11, 9, 30, 15, tzinfo=dt.timezone.utc),
    "since": dt.date(2026, 1, 1),
    "quiet_from": dt.time(22, 0),
    "cooldown": dt.timedelta(minutes=5),
    "seen_ids": {3, 1, 2},
    "range": (0.5, 1.5),
    "per_hour": {9: 4, 17: 6},
    "count": 7,
}


def assert_same(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Equal, and of the same types: a tuple that came back a list is not the same."""
    assert actual == expected
    for key, value in expected.items():
        assert type(actual[key]) is type(value), key


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    """A running node, with whatever it started stopped again afterwards."""
    runner = RecordingRunner(tmp_path)
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


class TestOnANode:
    def test_its_state_file_keeps_them(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_state.json"
        JsonState(path, "keeper").save(dict(VALUES))

        assert_same(JsonState(path, "keeper").load(), VALUES)

    def test_the_file_is_still_json(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_state.json"
        JsonState(path, "keeper").save(dict(VALUES))

        assert json.loads(path.read_text(encoding="utf-8"))["count"] == 7

    def test_a_file_of_plain_json_reads_as_it_always_did(self, tmp_path: Path) -> None:
        path = tmp_path / "agent_state.json"
        path.write_text(json.dumps({"count": 7, "readings": [1, 2]}), encoding="utf-8")

        assert JsonState(path, "keeper").load() == {"count": 7, "readings": [1, 2]}

    async def test_an_agent_started_again_remembers_them(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "keeper", "code": CODE})
        agent = runner.get("keeper")
        assert agent is not None
        for key, value in VALUES.items():
            agent.persist(key, value)

        await runner.stop_agent("keeper")
        await runner.spawn_agent({"name": "keeper", "code": CODE})

        again = runner.get("keeper")
        assert again is not None
        assert_same({key: again.recall(key) for key in VALUES}, VALUES)


class TestGoingOut:
    def test_nothing_is_left_behind(self) -> None:
        assert cannot_travel(VALUES, code_allowed=False) == []
        assert pack(VALUES, code_allowed=False).left_behind == []

    def test_a_move_is_not_refused_for_them(self) -> None:
        refusal = why_it_cannot_travel(
            json_part(VALUES),
            cannot_travel(VALUES, code_allowed=False),
            force=False,
            max_bytes=None,
        )

        assert refusal is None

    def test_what_is_sent_is_json(self) -> None:
        json.dumps(pack(VALUES, code_allowed=False).state)

    def test_json_safe_keeps_them(self) -> None:
        kept, dropped = json_safe(VALUES)

        assert dropped == []
        assert_same(decode_state(json.loads(json.dumps(kept))), VALUES)


class TestArrivingOnANode:
    async def test_the_agent_starts_with_them(self, runner: RecordingRunner) -> None:
        state = pack(VALUES, code_allowed=True).state

        await runner.spawn_agent({"name": "keeper", "code": CODE, "_initial_state": state})

        agent = runner.get("keeper")
        assert agent is not None
        assert_same({key: agent.recall(key) for key in VALUES}, VALUES)

    async def test_beside_a_blob_too(self, runner: RecordingRunner) -> None:
        # With a blob the snapshot is unpacked first, on its way in.
        packed = pack({**VALUES, "weights": np.arange(3.0)}, code_allowed=True)
        for sha, data in packed.blobs.items():
            for chunk in chunks(data):
                await runner.inbox.accept(sha, chunk)

        await runner.spawn_agent({"name": "keeper", "code": CODE, "_initial_state": packed.state})

        agent = runner.get("keeper")
        assert agent is not None
        assert_same({key: agent.recall(key) for key in VALUES}, VALUES)
        np.testing.assert_array_equal(agent.recall("weights"), np.arange(3.0))


class TestHandedBackByANode:
    async def test_the_state_it_sends_reads_back_as_them(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "keeper", "code": CODE})
        agent = runner.get("keeper")
        assert agent is not None
        for key, value in VALUES.items():
            agent.persist(key, value)

        await runner._migrate_agent({"name": "keeper", "target_node": "@main", "return_token": "t"})

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert returned["state_keys_dropped"] == []
        assert_same(decode_state(json.loads(json.dumps(returned["state"]))), VALUES)


class TestArrivingOnMain:
    async def test_its_stores_keep_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, store = WactorzDB(tmp_path / "wactorz.db"), PickleStore(str(tmp_path))
        monkeypatch.setattr(spawning, "get_db", lambda: db)
        monkeypatch.setattr(spawning, "get_pickle_store", lambda: store)
        sent = json.loads(json.dumps(pack(VALUES, code_allowed=False).state))

        await MainHost(FakeRegistry())._apply_initial_state(
            "keeper", {"name": "keeper", "_initial_state": sent}
        )

        assert_same(store.load("keeper"), VALUES)
