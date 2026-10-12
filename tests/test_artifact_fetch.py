"""A node fetches the models its agents name, once, and keeps them.

A spawn names artifacts by reference; main pins each to the version and the
bytes it means when the spawn goes out. The node asks for what it does not hold,
main sends only what an agent placed there names, and the node starts the agent
only once every file is here and hashes to what the signed spawn named.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from tests.test_node_runner import CODE, RecordingRunner
from tests.test_spawn_remote import _Main as SpawningMain
from tests.waiting import until
from wactorz.agents.main import artifact_service as service_module
from wactorz.agents.main.artifact_service import ArtifactService
from wactorz.agents.main.hosts import ArtifactHost
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import (
    ArtifactError,
    ArtifactStore,
    check_pinned,
    pinned,
    pinned_in,
)
from wactorz.core.blob_transfer import chunks, send_file
from wactorz.node import runner as runner_module
from wactorz.node.artifact_cache import ArtifactCache

MODEL = b"onnx bytes " * 100


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """Main's store, where the service looks for it."""
    state = tmp_path / "main-state"
    monkeypatch.setattr(service_module, "resolve_state_dir", lambda: str(state))
    return ArtifactStore.in_state_dir(state)


@pytest.fixture(name="stored")
def stored_fixture(store: ArtifactStore, tmp_path: Path) -> dict[str, Any]:
    """``imu-detector:1`` in the store, pinned."""
    path = tmp_path / "detector.onnx"
    path.write_bytes(MODEL)
    return pinned(store.add(path, "imu-detector"))


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    runner = RecordingRunner(tmp_path / "node-state")
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


class _Migration:
    """The migrations in flight, which is all the service reads of them."""

    def __init__(self) -> None:
        self.pending_spawns: dict[str, dict[str, Any]] = {}


class _Host:
    """Main, as the artifact service sees it: where agents are, and a broker."""

    def __init__(self, registry: dict[str, dict[str, Any]] | None = None) -> None:
        self.name = "main"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self.registry = dict(registry or {})
        self.migration = _Migration()
        self.published: list[tuple[str, bytes]] = []
        #: Where what is published goes as well, standing in for the broker.
        self.deliver: Any = None

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.registry

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))
        if self.deliver is not None:
            await self.deliver(topic, payload)


def service_for(host: _Host) -> ArtifactService:
    # The fake declares what the service reads; it stands in for MainActor.
    return ArtifactService(cast("ArtifactHost", host))


def request(*shas: str, agent: str = "detector") -> bytes:
    return json.dumps({"agent": agent, "sha256": list(shas)}).encode()


def reassembled(published: list[tuple[str, bytes]], topic: str) -> bytes:
    return b"".join(chunk[8:] for t, chunk in published if t == topic)


# ── Pinning ───────────────────────────────────────────────────────────────────


class TestPinning:
    def test_a_reference_is_pinned_to_its_version_and_bytes(
        self, store: ArtifactStore, stored: dict[str, Any]
    ) -> None:
        assert store.pin("imu-detector") == stored
        assert stored["sha256"] == sha(MODEL)
        assert (stored["version"], stored["format"], stored["size"]) == (1, "onnx", len(MODEL))

    def test_a_pinned_entry_stays_on_its_version_after_a_newer_one(
        self, store: ArtifactStore, stored: dict[str, Any], tmp_path: Path
    ) -> None:
        newer = tmp_path / "newer.onnx"
        newer.write_bytes(b"retrained")
        store.add(newer, "imu-detector")

        assert store.pin(stored) == stored
        assert store.pin("imu-detector")["version"] == 2

    def test_a_pinned_entry_whose_version_is_gone_is_refused(
        self, store: ArtifactStore, stored: dict[str, Any]
    ) -> None:
        store.remove("imu-detector:1")

        with pytest.raises(ArtifactError):
            store.pin(stored)

    def test_a_pinned_entry_naming_other_bytes_is_refused(
        self, store: ArtifactStore, stored: dict[str, Any]
    ) -> None:
        with pytest.raises(ArtifactError, match="no longer holds"):
            store.pin({**stored, "sha256": "0" * 64})

    @pytest.mark.parametrize("wanted", [["imu-detector"], "imu-detector", {"bad key!": "x"}])
    def test_artifacts_not_named_as_a_mapping_of_keys_are_refused(
        self, store: ArtifactStore, stored: dict[str, Any], wanted: Any
    ) -> None:
        with pytest.raises(ArtifactError):
            store.pin_all(wanted)

    @pytest.mark.parametrize(
        "change",
        [
            {"sha256": "nope"},
            {"size": -1},
            {"size": True},
            {"version": 0},
            {"name": "a/b"},
            {"format": "h5"},
        ],
    )
    def test_a_node_refuses_an_entry_that_is_not_well_formed(
        self, stored: dict[str, Any], change: dict[str, Any]
    ) -> None:
        with pytest.raises(ArtifactError):
            check_pinned({**stored, **change})

    def test_a_config_without_artifacts_names_none(self) -> None:
        assert pinned_in({"name": "x"}) == {}


# ── Main sending a spawn that names artifacts ─────────────────────────────────


class TestSpawningOnANode:
    @pytest.fixture(name="main")
    def main_fixture(self, store: ArtifactStore) -> SpawningMain:
        main = SpawningMain()
        main.actor.artifacts = ArtifactService(main.actor)
        return main

    async def test_what_is_sent_and_saved_names_the_pinned_version(
        self, main: SpawningMain, stored: dict[str, Any]
    ) -> None:
        config = {"name": "detector", "code": CODE, "artifacts": {"model": "imu-detector"}}

        await main.spawn(config)

        _, sent, _ = main.published_to("/spawn")
        assert sent["artifacts"] == {"model": stored}
        assert main.saved[0]["artifacts"] == {"model": stored}

    async def test_the_callers_config_is_left_alone(
        self, main: SpawningMain, stored: dict[str, Any]
    ) -> None:
        config = {"name": "detector", "code": CODE, "artifacts": {"model": "imu-detector"}}

        await main.spawn(config)

        assert config["artifacts"] == {"model": "imu-detector"}

    async def test_the_node_may_then_ask_for_it(
        self, main: SpawningMain, stored: dict[str, Any]
    ) -> None:
        await main.spawn({"name": "d", "code": CODE, "artifacts": {"model": "imu-detector"}})

        # Offered at once: the node may ask before the spawn is saved.
        assert main.actor.artifacts.offered == {"rpi": {stored["sha256"]}}

    async def test_an_artifact_that_is_not_stored_sends_nothing(
        self, main: SpawningMain, store: ArtifactStore
    ) -> None:
        await main.spawn({"name": "d", "code": CODE, "artifacts": {"model": "missing:1"}})

        assert not [row for row in main.published if row[0].endswith("/spawn")]
        (log,) = [p for t, p, _ in main.published if t.endswith("/logs")]
        assert "missing:1" in log["message"]
        assert not main.saved

    async def test_one_larger_than_a_node_accepts_sends_nothing(
        self, main: SpawningMain, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_module, "ARTIFACT_MAX_BYTES", 10)

        await main.spawn({"name": "d", "code": CODE, "artifacts": {"model": "imu-detector"}})

        assert not [row for row in main.published if row[0].endswith("/spawn")]


# ── Main answering a node ─────────────────────────────────────────────────────


class TestAnsweringANode:
    async def test_an_artifact_an_agent_there_names_is_sent(self, stored: dict[str, Any]) -> None:
        host = _Host({"d": {"name": "d", "node": "rpi", "artifacts": {"model": stored}}})

        await service_for(host).receive_request(
            "nodes/rpi/artifact_request", request(stored["sha256"])
        )

        topic = f"nodes/rpi/blob/{stored['sha256']}"
        assert reassembled(host.published, topic) == MODEL

    async def test_one_no_agent_there_names_is_not(
        self, stored: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        # Placed on another node: this one may not have it.
        host = _Host({"d": {"name": "d", "node": "nuc", "artifacts": {"model": stored}}})

        with caplog.at_level(logging.WARNING, logger=service_module.__name__):
            await service_for(host).receive_request(
                "nodes/rpi/artifact_request", request(stored["sha256"])
            )

        assert not host.published
        assert "none of its agents names" in caplog.text

    async def test_a_migration_waiting_on_the_node_counts(self, stored: dict[str, Any]) -> None:
        host = _Host()
        host.migration.pending_spawns = {
            "tok": {"target_node": "rpi", "config": {"name": "d", "artifacts": {"m": stored}}}
        }

        await service_for(host).receive_request(
            "nodes/rpi/artifact_request", request(stored["sha256"])
        )

        assert host.published

    async def test_one_no_longer_stored_is_not_sent(
        self, store: ArtifactStore, stored: dict[str, Any]
    ) -> None:
        host = _Host({"d": {"name": "d", "node": "rpi", "artifacts": {"model": stored}}})
        store.remove("imu-detector:1")

        await service_for(host).receive_request(
            "nodes/rpi/artifact_request", request(stored["sha256"])
        )

        assert not host.published

    @pytest.mark.parametrize(
        ("topic", "payload"),
        [
            ("nodes/rpi/artifact_request", b"not json"),
            ("nodes/rpi/artifact_request", b'{"sha256": "not a list"}'),
            ("nodes/rpi/artifact_request", b"[]"),
            ("nodes/rpi/extra/artifact_request", None),
            ("nodes/rpi/artifact_request", b""),
        ],
    )
    async def test_what_is_not_a_request_is_ignored(
        self, stored: dict[str, Any], topic: str, payload: bytes | None
    ) -> None:
        host = _Host({"d": {"name": "d", "node": "rpi", "artifacts": {"model": stored}}})
        body = payload if payload is not None else request(stored["sha256"])

        await service_for(host).receive_request(topic, body)

        assert not host.published


# ── The node fetching ─────────────────────────────────────────────────────────


def spawn_naming(entry: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"name": "detector", "code": CODE, "artifacts": {"model": entry}, **over}


def requests_made(runner: RecordingRunner) -> list[dict[str, Any]]:
    return [p for t, p, _ in runner.published if t.endswith("/artifact_request")]


async def deliver(runner: RecordingRunner, data: bytes) -> None:
    topic = f"nodes/{runner.node_name}/blob/{sha(data)}"
    for chunk in chunks(data):
        await runner._accept_blob(topic, chunk)


class TestTheNodeFetching:
    async def test_it_asks_for_what_it_lacks_and_starts_once_it_is_here(
        self, runner: RecordingRunner, stored: dict[str, Any]
    ) -> None:
        spawning = asyncio.create_task(runner.spawn_agent(spawn_naming(stored)))
        await until(lambda: requests_made(runner), "the node to ask for the model")

        (asked,) = requests_made(runner)
        assert asked == {"agent": "detector", "sha256": [stored["sha256"]]}
        assert runner.get("detector") is None, "not started before its model is here"

        await deliver(runner, MODEL)
        await spawning

        assert runner.get("detector") is not None
        assert runner.artifacts.path(stored["sha256"]).read_bytes() == MODEL

    async def test_what_is_cached_is_not_asked_for_again(
        self, runner: RecordingRunner, stored: dict[str, Any]
    ) -> None:
        runner.artifacts.directory.mkdir(parents=True)
        runner.artifacts.path(stored["sha256"]).write_bytes(MODEL)

        await runner.spawn_agent(spawn_naming(stored))

        assert not requests_made(runner)
        assert runner.get("detector") is not None

    async def test_a_cached_file_that_no_longer_matches_is_fetched_again(
        self, runner: RecordingRunner, stored: dict[str, Any]
    ) -> None:
        runner.artifacts.directory.mkdir(parents=True)
        runner.artifacts.path(stored["sha256"]).write_bytes(b"damaged on the card")

        spawning = asyncio.create_task(runner.spawn_agent(spawn_naming(stored)))
        await until(lambda: requests_made(runner), "the node to ask for the model again")
        await deliver(runner, MODEL)
        await spawning

        assert runner.artifacts.path(stored["sha256"]).read_bytes() == MODEL

    async def test_one_that_never_arrives_leaves_the_agent_unstarted(
        self, runner: RecordingRunner, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner_module, "wait_for", lambda _bytes: 0.05)

        await runner.spawn_agent(spawn_naming(stored, _migration_token="t"))

        assert runner.get("detector") is None
        (answer,) = [p for t, p, _ in runner.published if t.endswith("/spawn_ack")]
        assert "did not arrive" in answer["refused"]

    async def test_bytes_that_do_not_match_are_not_used(
        self, runner: RecordingRunner, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner_module, "wait_for", lambda _bytes: 0.1)
        spawning = asyncio.create_task(runner.spawn_agent(spawn_naming(stored)))
        await until(lambda: requests_made(runner), "the node to ask")

        # Sent on the model's topic, but other bytes: they never hash to it.
        topic = f"nodes/{runner.node_name}/blob/{stored['sha256']}"
        for chunk in chunks(b"forged"):
            await runner._accept_blob(topic, chunk)
        await spawning

        assert runner.get("detector") is None
        assert not runner.artifacts.path(stored["sha256"]).exists()

    async def test_an_entry_that_is_not_well_formed_is_refused_without_asking(
        self, runner: RecordingRunner, stored: dict[str, Any]
    ) -> None:
        await runner.spawn_agent(spawn_naming({**stored, "sha256": "nope"}, _migration_token="t"))

        assert not requests_made(runner)
        (answer,) = [p for t, p, _ in runner.published if t.endswith("/spawn_ack")]
        assert "not named properly" in answer["refused"]

    async def test_one_over_the_nodes_limit_is_refused_without_asking(
        self, runner: RecordingRunner, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner_module, "ARTIFACT_MAX_BYTES", 10)

        await runner.spawn_agent(spawn_naming(stored))

        assert not requests_made(runner)
        assert runner.get("detector") is None

    async def test_an_agent_naming_none_starts_as_before(self, runner: RecordingRunner) -> None:
        await runner.spawn_agent({"name": "plain", "code": CODE})

        assert runner.get("plain") is not None
        assert not requests_made(runner)


class TestMainAndTheNodeTogether:
    """Main's real answer, carried to the node's real inbox, with no broker between."""

    async def test_an_agent_starts_with_the_model_main_holds(
        self, runner: RecordingRunner, stored: dict[str, Any]
    ) -> None:
        host = _Host({"detector": {"name": "detector", "node": "rpi", "artifacts": {"m": stored}}})

        async def to_node(topic: str, payload: Any) -> None:
            if topic.startswith(f"nodes/{runner.node_name}/blob/"):
                await runner._accept_blob(topic, payload)

        host.deliver = to_node
        service = service_for(host)

        spawning = asyncio.create_task(runner.spawn_agent(spawn_naming(stored)))
        await until(lambda: requests_made(runner), "the node to ask")
        (asked,) = requests_made(runner)
        await service.receive_request(
            f"nodes/{runner.node_name}/artifact_request", json.dumps(asked).encode()
        )
        await spawning

        assert runner.get("detector") is not None
        assert runner.artifacts.path(stored["sha256"]).read_bytes() == MODEL


# ── The cache ─────────────────────────────────────────────────────────────────


def cached(cache: ArtifactCache, data: bytes, age_s: float) -> str:
    cache.directory.mkdir(parents=True, exist_ok=True)
    path = cache.path(sha(data))
    path.write_bytes(data)
    when = path.stat().st_mtime - age_s
    os.utime(path, (when, when))
    return sha(data)


class TestTheCache:
    def test_what_is_over_the_limit_and_named_by_no_agent_goes_oldest_first(
        self, tmp_path: Path
    ) -> None:
        cache = ArtifactCache(tmp_path, max_bytes=20)
        oldest = cached(cache, b"a" * 10, age_s=300)
        older = cached(cache, b"b" * 10, age_s=200)
        newest = cached(cache, b"c" * 10, age_s=100)

        removed = cache.trim(named=set())

        assert removed == [oldest]
        assert {p.name for p in tmp_path.iterdir()} == {older, newest}

    def test_one_an_agent_names_stays_whatever_the_total(self, tmp_path: Path) -> None:
        cache = ArtifactCache(tmp_path, max_bytes=5)
        named = cached(cache, b"a" * 10, age_s=300)
        other = cached(cache, b"b" * 10, age_s=100)

        assert cache.trim(named={named}) == [other]
        assert cache.path(named).exists()

    def test_no_limit_keeps_everything(self, tmp_path: Path) -> None:
        cache = ArtifactCache(tmp_path, max_bytes=0)
        cached(cache, b"a" * 10, age_s=300)

        assert cache.trim(named=set()) == []

    def test_a_file_used_is_the_last_to_go(self, tmp_path: Path) -> None:
        cache = ArtifactCache(tmp_path, max_bytes=10)
        used = cached(cache, b"a" * 10, age_s=300)
        idle = cached(cache, b"b" * 10, age_s=100)

        cache.used([used])

        assert cache.trim(named=set()) == [idle]

    def test_it_outlives_the_node(self, tmp_path: Path) -> None:
        # A file is all it is: a node started again on the same state finds it.
        kept = cached(ArtifactCache(tmp_path, 0), MODEL, age_s=0)

        assert ArtifactCache(tmp_path, 0).usable([kept]) == {kept}

    def test_the_node_asks_with_the_runner_limit_in_force(self) -> None:
        assert runner_module._largest(64, 256) == 256
        assert runner_module._largest(0, 256) == runner_module.sys.maxsize


async def test_a_file_is_sent_a_chunk_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "big"
    data = os.urandom(3 * 256 * 1024 + 7)
    path.write_bytes(data)
    sent: list[bytes] = []

    async def publish(_topic: str, chunk: bytes) -> None:
        sent.append(chunk)

    await send_file(path, "t", publish)

    assert len(sent) == 4
    assert b"".join(chunk[8:] for chunk in sent) == data
    assert sent == list(chunks(data))
