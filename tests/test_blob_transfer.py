"""An agent's blobs travel beside its state when it moves.

A migration ships the state as JSON; a model, an array or raw bytes goes as a
reference naming its hash, and its bytes follow in chunks on a topic of their
own. The receiver uses a blob only when its bytes match the hash a trusted
message named, and main loads nothing from a node that runs code when loaded.
"""

import asyncio
import hashlib
import json
import sys
import types
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.test_migrate_agent import TestGoingOut, _Main, online, with_code
from tests.test_node_runner import CODE, RecordingRunner
from wactorz.agents.main import migration as migration_module
from wactorz.agents.main.migration import TOKEN_TTL_S
from wactorz.core import blob_transfer
from wactorz.core.blob_transfer import (
    CHUNK_BYTES,
    BlobsUnusable,
    Inbox,
    cannot_travel,
    chunks,
    is_reference,
    pack,
    reference,
    sha256_of_topic,
    to_node_topic,
    unpack,
)
from wactorz.core.blobs import BLOB_MARK
from wactorz.core.paths import agent_state_dir
from wactorz.core.state_snapshot import why_it_cannot_travel

BIG = 1 << 40


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _deliver(inbox: Inbox, data: bytes) -> str:
    sha = _sha(data)
    for chunk in chunks(data):
        await inbox.accept(sha, chunk)
    return sha


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


@pytest.fixture(name="sklearn_model")
def sklearn_model_fixture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A model of a family saved with joblib, which runs code when it loads."""
    monkeypatch.setitem(sys.modules, "sklearn", types.ModuleType("sklearn"))
    return type("Forest", (), {"__module__": "sklearn.ensemble"})()


class TestPacking:
    def test_json_stays_and_a_blob_becomes_a_reference(self) -> None:
        packed = pack({"count": 2, "weights": np.arange(3), "raw": b"\x00"}, code_allowed=False)

        assert packed.state["count"] == 2
        assert is_reference(packed.state["weights"]) and is_reference(packed.state["raw"])
        assert packed.state["raw"]["sha256"] == _sha(b"\x00")
        assert set(packed.blobs) == {
            r["sha256"] for r in (packed.state["weights"], packed.state["raw"])
        }
        assert packed.left_behind == []
        json.dumps(packed.state)

    def test_what_has_no_form_to_travel_in_is_left_behind(self) -> None:
        packed = pack({"count": 2, "capture": object()}, code_allowed=True)

        assert packed.state == {"count": 2}
        assert packed.left_behind == ["capture"]

    def test_what_runs_code_is_left_behind_where_that_is_not_allowed(
        self, sklearn_model: Any
    ) -> None:
        assert cannot_travel({"model": sklearn_model}, code_allowed=False) == ["model"]
        assert cannot_travel({"model": sklearn_model}, code_allowed=True) == []
        assert pack({"model": sklearn_model}, code_allowed=False).left_behind == ["model"]

    def test_unpacking_puts_the_values_back(self, tmp_path: Path) -> None:
        packed = pack({"count": 2, "weights": np.arange(3.0)}, code_allowed=False)
        received = {}
        for sha, data in packed.blobs.items():
            (tmp_path / sha).write_bytes(data)
            received[sha] = tmp_path / sha

        values, refused = unpack(packed.state, received, code_allowed=False)

        assert values["count"] == 2 and refused == []
        np.testing.assert_array_equal(values["weights"], np.arange(3.0))

    def test_unpacking_refuses_what_runs_code_where_that_is_not_allowed(self) -> None:
        state = {"model": {BLOB_MARK: "joblib", "sha256": "0" * 64, "size": 1}}

        values, refused = unpack(state, {}, code_allowed=False)

        assert values == {} and refused == ["model"]

    @pytest.mark.parametrize(
        "state",
        [
            {"raw": {BLOB_MARK: "bytes", "sha256": "a" * 64, "size": 1}},
            {"raw": {BLOB_MARK: "from-the-future", "sha256": "a" * 64, "size": 1}},
        ],
        ids=["never arrived", "unknown format"],
    )
    def test_a_blob_that_cannot_be_put_back_stops_the_arrival(self, state: Any) -> None:
        with pytest.raises(BlobsUnusable):
            unpack(state, {}, code_allowed=True)

    def test_a_marker_kept_on_disk_is_not_a_reference(self) -> None:
        assert not is_reference({BLOB_MARK: "bytes"})
        assert not is_reference({BLOB_MARK: "bytes", "sha256": "../x", "size": 1})
        assert is_reference(reference("bytes", b"x"))


class TestTheLimit:
    def test_blobs_over_it_are_refused_however_forced(self) -> None:
        reason = why_it_cannot_travel(
            {}, [], force=True, max_bytes=None, blob_bytes=11, max_blob_bytes=10
        )
        assert reason is not None and "WACTORZ_MIGRATION_MAX_BLOB_BYTES" in reason

    def test_no_limit_is_no_limit(self) -> None:
        assert why_it_cannot_travel({}, [], force=False, max_bytes=None, blob_bytes=11) is None


class TestTopics:
    def test_a_topic_names_its_blob_by_hash_only(self) -> None:
        sha = _sha(b"x")
        assert sha256_of_topic(to_node_topic("rpi", sha)) == sha
        assert sha256_of_topic("nodes/rpi/blob/..") is None
        assert sha256_of_topic("nodes/rpi/blob/" + "A" * 64) is None


class TestTheInbox:
    async def test_a_blob_in_several_chunks_arrives_whole(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        data = bytes(range(256)) * (CHUNK_BYTES // 128)  # two chunks

        sha = await _deliver(inbox, data)

        assert len(list(chunks(data))) == 2
        assert inbox.path(sha).read_bytes() == data

    async def test_bytes_that_do_not_match_their_hash_are_dropped(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        claimed = _sha(b"what was named")

        for chunk in chunks(b"something else"):
            await inbox.accept(claimed, chunk)

        assert not any(tmp_path.iterdir())

    async def test_a_chunk_out_of_order_drops_the_blob(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        data = b"x" * (CHUNK_BYTES * 3)
        first, second, third = chunks(data)

        for chunk in (first, third, second):
            await inbox.accept(_sha(data), chunk)

        assert not inbox.path(_sha(data)).exists()

    async def test_a_chunk_delivered_twice_is_taken_once(self, tmp_path: Path) -> None:
        # QoS 1: the broker sends a message again when it did not hear that it
        # arrived, as across a reconnect.
        inbox = Inbox(tmp_path, BIG)
        data = bytes(range(256)) * (CHUNK_BYTES // 64)  # four chunks
        first, second, third, fourth = chunks(data)

        for chunk in (first, second, second, third, first, fourth):
            await inbox.accept(_sha(data), chunk)

        assert inbox.path(_sha(data)).read_bytes() == data

    async def test_a_blob_sent_again_part_way_carries_on_where_it_was(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        data = b"y" * (CHUNK_BYTES * 3)
        first, second, third = chunks(data)
        await inbox.accept(_sha(data), first)
        await inbox.accept(_sha(data), second)

        for chunk in (first, second, third):
            await inbox.accept(_sha(data), chunk)

        assert inbox.path(_sha(data)).read_bytes() == data

    async def test_a_blob_whose_first_chunk_never_came_is_not_started(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        data = b"z" * (CHUNK_BYTES * 2)
        _first, second = chunks(data)

        await inbox.accept(_sha(data), second)

        assert not any(tmp_path.glob("*"))

    async def test_a_blob_over_the_limit_is_dropped(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, CHUNK_BYTES)
        data = b"x" * (CHUNK_BYTES * 3)

        await _deliver(inbox, data)

        assert not any(tmp_path.glob("*"))

    async def test_waiting_ends_when_the_last_blob_arrives(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        data = b"late"
        waiting = asyncio.create_task(inbox.wait_for({_sha(data)}, timeout=30))
        await asyncio.sleep(0)
        assert not waiting.done()

        await _deliver(inbox, data)

        assert await waiting == {_sha(data): inbox.path(_sha(data))}

    async def test_waiting_for_one_that_never_comes_says_so(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)

        with pytest.raises(BlobsUnusable, match="1 of 1"):
            await inbox.wait_for({_sha(b"never")}, timeout=0.05)

    async def test_released_and_old_blobs_are_removed(self, tmp_path: Path) -> None:
        inbox = Inbox(tmp_path, BIG)
        used = await _deliver(inbox, b"used")
        old = await _deliver(inbox, b"old")

        await inbox.release({used})
        await inbox.sweep(older_than_s=-1)

        assert not inbox.path(used).exists() and not inbox.path(old).exists()


class TestANodeReceivingAnAgent:
    async def test_the_blob_is_in_its_state_when_it_starts(self, runner: RecordingRunner) -> None:
        packed = pack({"count": 1, "raw": b"weights"}, code_allowed=True)
        for data in packed.blobs.values():
            await _deliver(runner.inbox, data)

        await runner.spawn_agent(
            {"name": "collector", "code": CODE, "_initial_state": packed.state}
        )

        agent = runner.get("collector")
        assert agent is not None
        assert agent.recall("raw") == b"weights"
        assert agent.recall("count") == 1

    async def test_without_its_blob_it_is_not_started(
        self, runner: RecordingRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.node.runner.wait_for", lambda _bytes: 0.05)
        state = {"raw": reference("bytes", b"never sent")}

        await runner.spawn_agent(
            {"name": "collector", "code": CODE, "_initial_state": state, "_migration_token": "t"}
        )

        assert runner.get("collector") is None
        assert not [t for t in runner.topics if t.endswith("/spawn_ack")]


class TestANodeHandingAnAgentBack:
    async def test_the_blob_goes_ahead_of_the_state_that_names_it(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("raw", b"weights")

        await runner._migrate_agent({"name": "collector", "target_node": "@main"})

        topics = runner.topics
        blob_at = next(i for i, t in enumerate(topics) if "/blob_return/" in t)
        return_at = next(i for i, t in enumerate(topics) if t.endswith("/state_return"))
        assert blob_at < return_at
        returned = runner.published[return_at][1]
        assert returned["state"]["raw"]["sha256"] == _sha(b"weights")
        assert topics[blob_at] == f"nodes/rpi/blob_return/{_sha(b'weights')}"

    async def test_what_runs_code_on_main_stays_unless_forced(
        self, runner: RecordingRunner, sklearn_model: Any
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent._persistent_state = {"count": 1, "model": sklearn_model}

        await runner._migrate_agent({"name": "collector", "target_node": "@main"})

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert "model" in returned["refused"]
        assert runner.get("collector") is agent

    async def test_blobs_over_the_limit_main_sets_start_it_here_again(
        self, runner: RecordingRunner
    ) -> None:
        await runner.spawn_agent({"name": "collector", "code": CODE})
        agent = runner.get("collector")
        assert agent is not None
        agent.persist("raw", b"x" * 100)

        await runner._migrate_agent(
            {"name": "collector", "target_node": "@main", "max_blob_bytes": 10}
        )

        (returned,) = [p for t, p, _ in runner.published if t.endswith("/state_return")]
        assert "WACTORZ_MIGRATION_MAX_BLOB_BYTES" in returned["refused"]
        assert not [t for t in runner.topics if "/blob_return/" in t]
        restarted = runner.get("collector")
        assert restarted is not None and restarted.recall("raw") == b"x" * 100


class TestMainSendingAnAgentOut:
    async def test_its_blob_follows_the_spawn_that_names_it(self) -> None:
        main = TestGoingOut._main()
        main.local().holding(count=1, raw=b"weights")

        result = await main.migrate("collector", "nuc")

        assert result["success"], result
        (config, _node, _save), *_ = main.spawned_remote
        ref = config["_initial_state"]["raw"]
        assert ref["sha256"] == _sha(b"weights")
        (chunk,) = [p for t, p in main.published if t == f"nodes/nuc/blob/{ref['sha256']}"]
        assert chunk.endswith(b"weights")

    async def test_the_wait_for_its_confirmation_allows_for_the_blobs(self) -> None:
        main = TestGoingOut._main()
        main.local().holding(raw=b"x" * 1000)

        await main.migrate("collector", "nuc")

        (entry,) = main.actor.migration.pending_spawns.values()
        assert entry["ttl"] > TOKEN_TTL_S


class TestMainTakingAnAgentBack:
    @staticmethod
    async def _returned(
        main: _Main, inbox: Inbox, state: dict[str, Any], target: str = "local"
    ) -> None:
        main.actor.migration._inbox = inbox
        await main.migrate("collector", target)
        ((_topic, request),) = main.published_to("/migrate")
        await main.actor.migration.receive_state_return(
            "nodes/rpi/state_return",
            json.dumps(
                {
                    "agent": "collector",
                    "return_token": request["return_token"],
                    "config": with_code("rpi"),
                    "state": state,
                }
            ).encode(),
        )
        await asyncio.gather(*main.actor.migration._returns)

    async def test_it_starts_here_with_its_blob(self, tmp_path: Path) -> None:
        main = _Main(spawn_registry={"collector": with_code("rpi")}, nodes={"rpi": online()})
        inbox = Inbox(tmp_path, BIG)
        await _deliver(inbox, b"weights")

        await self._returned(main, inbox, {"count": 1, "raw": reference("bytes", b"weights")})

        (spawned,) = main.spawned_local
        assert spawned["_initial_state"] == {"count": 1, "raw": b"weights"}
        assert not inbox.path(_sha(b"weights")).exists(), "used, then removed"

    async def test_what_would_run_code_here_is_left_behind(self, tmp_path: Path) -> None:
        main = _Main(spawn_registry={"collector": with_code("rpi")}, nodes={"rpi": online()})
        model = {BLOB_MARK: "joblib", "sha256": "0" * 64, "size": 1}

        await self._returned(main, Inbox(tmp_path, BIG), {"count": 1, "model": model})

        (spawned,) = main.spawned_local
        assert spawned["_initial_state"] == {"count": 1}
        assert "model" in main.notifications[-1]["message"]

    async def test_a_blob_that_never_arrives_restarts_it_where_it_was(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(migration_module, "wait_for", lambda _bytes: 0.05)
        main = _Main(spawn_registry={"collector": with_code("rpi")}, nodes={"rpi": online()})

        await self._returned(main, Inbox(tmp_path, BIG), {"raw": reference("bytes", b"lost")})

        assert not main.spawned_local
        ((restored, node, _save),) = main.spawned_remote
        assert node == "rpi" and restored["node"] == "rpi"
        assert "_initial_state" not in restored
        assert "failed" in main.notifications[-1]["message"]

    async def test_a_failure_while_placing_it_restarts_it_where_it_was(
        self, tmp_path: Path
    ) -> None:
        main = _Main(spawn_registry={"collector": with_code("rpi")}, nodes={"rpi": online()})
        inbox = Inbox(tmp_path, BIG)
        await _deliver(inbox, b"weights")

        async def broken(*_args: Any, **_kw: Any) -> bool:
            raise RuntimeError("something nobody expected")

        setattr(main.actor.migration, "_respawn_locally", broken)

        await self._returned(main, inbox, {"raw": reference("bytes", b"weights")})

        ((restored, node, _save),) = main.spawned_remote
        assert node == "rpi" and "_initial_state" not in restored
        assert "failed" in main.notifications[-1]["message"]

    async def test_on_its_way_to_another_node_the_blob_is_passed_on(self, tmp_path: Path) -> None:
        main = _Main(
            spawn_registry={"collector": with_code("rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )
        inbox = Inbox(tmp_path, BIG)
        await _deliver(inbox, b"weights")

        await self._returned(main, inbox, {"raw": reference("bytes", b"weights")}, target="nuc")

        ((config, node, _save),) = main.spawned_remote
        assert node == "nuc"
        assert config["_initial_state"]["raw"]["sha256"] == _sha(b"weights")
        assert [t for t, _p in main.published if t == f"nodes/nuc/blob/{_sha(b'weights')}"]


def test_the_inbox_is_a_directory_no_agent_can_be_given(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        agent_state_dir(tmp_path, blob_transfer.INBOX_DIRNAME)
