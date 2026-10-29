"""The history of a model's pointers: each move an action in the provenance graph.

The store journals each change as it is made, whoever made it -- the
dashboard, the chat, the CLI beside the server -- main writes the journal into
the graph on its next look, and the models routes read them back newest first. The index itself stays where each pointer is now; see
``wactorz.core.pointer_moves`` for why, and for what is recorded of a move made
while main was away.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.agents.main import artifact_service as service_module
from wactorz.agents.main.artifact_service import ArtifactService
from wactorz.agents.main.hosts import ArtifactHost
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import ArtifactStore, Pointer
from wactorz.core.persistence import WactorzDB
from wactorz.core.pointer_moves import (
    CLEARED,
    MOVED,
    PROMOTED,
    history,
    record_clear,
    record_move,
    source_of,
)
from wactorz.core.provenance import ACTION, DONE, Provenance
from wactorz.web import api_models, runtime
from wactorz.web.app import build_app

T0 = 1_760_000_000.0


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """``imu`` in three versions, in the store main and the routes look in."""
    state = tmp_path / "state"
    monkeypatch.setattr(service_module, "resolve_state_dir", lambda: str(state))
    monkeypatch.setattr(api_models, "resolve_state_dir", lambda: str(state))
    store = ArtifactStore.in_state_dir(state)
    for version in (1, 2, 3):
        weights = tmp_path / f"imu-{version}.onnx"
        weights.write_bytes(f"imu weights {version}".encode())
        store.add(weights, "imu")
    return store


def _pointer(version: int, *, which: str = "active", previous: int | None = None) -> Pointer:
    return Pointer(
        name="imu",
        pointer=which,
        version=version,
        sha256=f"{version:064x}",
        previous=previous,
        moved_at=T0 + version,
        by="cli",
        reason=f"to {version}",
    )


class TestTheRecord:
    def test_a_move_is_a_done_action_about_the_model_by_who_moved_it(
        self, graph: Provenance
    ) -> None:
        node = record_move(graph, _pointer(3, previous=2))

        assert (node.kind, node.status, node.source, node.actor) == (
            ACTION,
            DONE,
            source_of("imu"),
            "cli",
        )
        assert node.ts == T0 + 3
        assert node.payload["change"] == MOVED
        assert (node.payload["version"], node.payload["from"]) == (3, 2)

    def test_the_same_move_recorded_twice_is_one_node(self, graph: Provenance) -> None:
        record_move(graph, _pointer(3, previous=2))
        record_move(graph, _pointer(3, previous=2))

        assert len(graph.nodes(ACTION)) == 1

    def test_the_history_is_newest_first_by_when_each_happened(self, graph: Provenance) -> None:
        # Written out of order, as a move found at start is written late.
        record_move(graph, _pointer(3, previous=2))
        record_move(graph, _pointer(2, previous=1))
        record_clear(graph, _pointer(1, which="shadow"), at=T0 + 10, by="dashboard")

        moves = history(graph, "imu")

        assert [(m["change"], m["pointer"], m["version"], m["from"]) for m in moves] == [
            (CLEARED, "shadow", None, 1),
            (MOVED, "active", 3, 2),
            (MOVED, "active", 2, 1),
        ]
        assert moves[0]["by"] == "dashboard"
        assert [m["version"] for m in history(graph, "imu", limit=1)] == [None]
        assert history(graph, "other") == []


class _Host:
    """Main, as the pointer watcher sees it: a name, an id, a state and a broker."""

    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self.published: list[tuple[str, Any, bool, int]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names({})

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload, retain, qos))


@pytest.fixture(name="service")
def service_fixture(db: WactorzDB, monkeypatch: pytest.MonkeyPatch) -> ArtifactService:
    monkeypatch.setattr(service_module, "get_db", lambda: db)
    # The fake declares what the service reads; it stands in for MainActor.
    return ArtifactService(cast("ArtifactHost", _Host()))


def _changes(graph: Provenance) -> list[tuple[str, str, Any, str]]:
    """What the history says, oldest first: change, pointer, version, by."""
    return [
        (m["change"], m["pointer"], m["version"], m["by"]) for m in reversed(history(graph, "imu"))
    ]


class TestMainRecordsWhatItSees:
    async def test_the_first_look_records_where_each_pointer_stands_once(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        # Moved while main was away: the first look writes it, a second does not.
        store.move("active", "imu:2", by="cli")

        await service.publish_pointers()
        await service.publish_pointers()

        assert _changes(graph) == [(MOVED, "active", 2, "cli")]

    async def test_each_move_after_is_recorded_with_who_made_it(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        await service.publish_pointers()

        store.move("active", "imu:1", by="dashboard", reason="first")
        await service.publish_pointers()
        store.move("shadow", "imu:3", by="chat")
        await service.publish_pointers()
        store.move("active", "imu:2", by="cli")
        await service.publish_pointers()
        store.rollback("imu", by="dashboard", reason="2 is worse")
        await service.publish_pointers()

        assert _changes(graph) == [
            (MOVED, "active", 1, "dashboard"),
            (MOVED, "shadow", 3, "chat"),
            (MOVED, "active", 2, "cli"),
            (MOVED, "active", 1, "dashboard"),
        ]
        assert history(graph, "imu")[0]["reason"] == "2 is worse"

    async def test_a_clear_is_recorded_by_whoever_cleared_it_here(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("shadow", "imu:3", by="cli")
        await service.publish_pointers()

        store.clear("shadow", "imu", by="dashboard")
        await service.publish_pointers()

        assert _changes(graph)[-1] == (CLEARED, "shadow", None, "dashboard")

    async def test_a_clear_made_by_another_process_is_recorded_by_whoever_made_it(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("shadow", "imu:3", by="cli")
        await service.publish_pointers()

        # As the CLI beside the server would: its own store, on the same directory.
        ArtifactStore(store.root).clear("shadow", "imu", by="cli", reason="noisy")
        await service.publish_pointers()

        assert _changes(graph)[-1] == (CLEARED, "shadow", None, "cli")
        assert history(graph, "imu")[0]["reason"] == "noisy"

    async def test_a_shadow_set_and_promoted_between_two_looks_keeps_both(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("active", "imu:1", by="cli")
        await service.publish_pointers()

        store.move("shadow", "imu:3", by="dashboard", reason="quieter at night")
        store.promote("imu", by="dashboard", reason="fewer false alarms")
        await service.publish_pointers()

        assert _changes(graph)[-3:] == [
            (MOVED, "shadow", 3, "dashboard"),
            (MOVED, "active", 3, "dashboard"),
            (PROMOTED, "shadow", None, "dashboard"),
        ]
        reasons = [move["reason"] for move in reversed(history(graph, "imu"))]
        assert reasons[-3:] == ["quieter at night", "fewer false alarms", "fewer false alarms"]

    async def test_a_clear_made_while_main_was_away_is_recorded(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("shadow", "imu:3", by="cli")
        store.clear("shadow", "imu", by="cli", reason="the wrong file")

        await service.publish_pointers()

        assert _changes(graph) == [(MOVED, "shadow", 3, "cli"), (CLEARED, "shadow", None, "cli")]
        assert history(graph, "imu")[0]["reason"] == "the wrong file"

    async def test_the_journal_is_trimmed_once_recorded(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("active", "imu:2", by="cli")

        await service.publish_pointers()
        await service.publish_pointers()

        assert store.pointer_changes() == ([], b"")
        assert _changes(graph) == [(MOVED, "active", 2, "cli")]

    async def test_a_journal_the_database_refused_is_recorded_on_the_next_look(
        self,
        store: ArtifactStore,
        service: ArtifactService,
        graph: Provenance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await service.publish_pointers()
        store.move("shadow", "imu:3", by="chat", reason="try it")

        def refuse(*_args: Any) -> None:
            raise RuntimeError("disk full")

        with monkeypatch.context() as patched:
            patched.setattr(service_module, "record_change", refuse)
            await service.publish_pointers()
        assert store.pointer_changes()[0], "kept for the next look"
        await service.publish_pointers()

        assert [move["reason"] for move in history(graph, "imu")] == ["try it"]
        assert store.pointer_changes() == ([], b"")

    async def test_a_promotion_clears_the_shadow_as_the_promotion(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("active", "imu:1", by="cli")
        store.move("shadow", "imu:3", by="cli")
        await service.publish_pointers()

        store.promote("imu", by="dashboard", reason="won the trial")
        await service.publish_pointers()

        assert _changes(graph)[-2:] == [
            (MOVED, "active", 3, "dashboard"),
            (PROMOTED, "shadow", None, "dashboard"),
        ]

    async def test_a_promotion_the_journal_lost_keeps_its_reason_from_the_index(
        self,
        store: ArtifactStore,
        service: ArtifactService,
        graph: Provenance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store.move("active", "imu:1", by="cli")
        store.move("shadow", "imu:3", by="cli")
        await service.publish_pointers()
        store.promote("imu", by="dashboard", reason="won the trial")
        store.journal_path.unlink()

        await service.publish_pointers()

        newest = history(graph, "imu")[:2]
        assert {(m["change"], m["reason"]) for m in newest} == {
            (MOVED, "won the trial"),
            (PROMOTED, "won the trial"),
        }

    async def test_a_database_that_refuses_the_record_does_not_repeat_the_change(
        self, store: ArtifactStore, service: ArtifactService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await service.publish_pointers()

        def refuse(*_args: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(service_module, "record_pointer_changes", refuse)
        store.move("active", "imu:2", by="cli")
        await service.publish_pointers()
        host = cast("_Host", service.host)
        published = len(host.published)
        await service.publish_pointers()

        assert len(host.published) == published, "the change is announced once"

    async def test_without_a_database_nothing_is_recorded_and_nothing_breaks(
        self, store: ArtifactStore, service: ArtifactService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_module, "get_db", lambda: None)
        store.move("active", "imu:2", by="cli")

        await service.publish_pointers()

        assert service.announced[("imu", "active")] is not None
        assert store.pointer_changes()[0], "the journal waits for a main that records it"


def _journalled(store: ArtifactStore) -> list[tuple[str, str, int, str, Any]]:
    """What the journal holds, oldest first: change, pointer, version, by, reason."""
    return [
        (c["change"], c["pointer"]["pointer"], c["pointer"]["version"], c["by"], c["reason"])
        for c in store.pointer_changes()[0]
    ]


class TestTheStoreJournalsEachChange:
    def test_each_move_and_clear_with_who_and_why(self, store: ArtifactStore) -> None:
        store.move("active", "imu:1", by="cli", reason="first")
        store.move("shadow", "imu:3", by="chat")
        store.promote("imu", by="dashboard", reason="won")
        store.rollback("imu", by="dashboard", reason="worse")
        store.move("shadow", "imu:2", by="cli")
        store.clear("shadow", "imu", by="cli", reason="noisy")

        assert _journalled(store) == [
            (MOVED, "active", 1, "cli", "first"),
            (MOVED, "shadow", 3, "chat", None),
            (MOVED, "active", 3, "dashboard", "won"),
            (PROMOTED, "shadow", 3, "dashboard", "won"),
            (MOVED, "active", 1, "dashboard", "worse"),
            (MOVED, "shadow", 2, "cli", None),
            (CLEARED, "shadow", 2, "cli", "noisy"),
        ]

    def test_active_moved_onto_the_shadow_version_is_a_promotion(
        self, store: ArtifactStore
    ) -> None:
        store.move("shadow", "imu:3", by="cli")

        store.move("active", "imu:3", by="chat", reason="it held up")

        assert _journalled(store)[-2:] == [
            (MOVED, "active", 3, "chat", "it held up"),
            (PROMOTED, "shadow", 3, "chat", "it held up"),
        ]

    def test_removing_every_version_clears_its_pointers(self, store: ArtifactStore) -> None:
        store.move("active", "imu:1", by="cli")
        store.move("shadow", "imu:2", by="cli")

        store.remove("imu", every_version=True)

        assert sorted(_journalled(store)[-2:]) == [
            (CLEARED, "active", 1, "", None),
            (CLEARED, "shadow", 2, "", None),
        ]

    def test_what_is_journalled_after_a_read_is_kept_when_the_read_is_forgotten(
        self, store: ArtifactStore
    ) -> None:
        store.move("active", "imu:1", by="cli")
        changes, read = store.pointer_changes()
        store.move("active", "imu:2", by="cli")

        store.forget_pointer_changes(read)

        assert len(changes) == 1
        assert _journalled(store) == [(MOVED, "active", 2, "cli", None)]

    def test_two_readers_of_the_same_lines_never_drop_a_newer_one(
        self, store: ArtifactStore
    ) -> None:
        # The watcher and a chat command may both read before either forgets.
        store.move("active", "imu:1", by="cli")
        _, first = store.pointer_changes()
        _, second = store.pointer_changes()
        store.move("active", "imu:2", by="cli")

        store.forget_pointer_changes(first)
        store.forget_pointer_changes(second)

        assert _journalled(store) == [(MOVED, "active", 2, "cli", None)]

    def test_a_line_cut_short_is_set_aside_and_the_next_change_kept_whole(
        self, store: ArtifactStore
    ) -> None:
        store.move("active", "imu:1", by="cli")
        with store.journal_path.open("ab") as journal:
            journal.write(b'{"change": "cleared", "at": 1, "poin')
        assert len(store.pointer_changes()[0]) == 1, "a line with no end yet is not read"

        store.move("shadow", "imu:3", by="chat")
        changes, read = store.pointer_changes()
        rejected = store.forget_pointer_changes(read)

        assert [(c["change"], c["by"]) for c in changes] == [(MOVED, "cli"), (MOVED, "chat")]
        assert rejected == [b'{"change": "cleared", "at": 1, "poin']
        assert store.rejected_journal_path.read_bytes() == rejected[0] + b"\n"
        assert store.pointer_changes() == ([], b"")

    async def test_a_line_that_is_not_a_change_is_passed_over(
        self, store: ArtifactStore, service: ArtifactService, graph: Provenance
    ) -> None:
        store.move("active", "imu:2", by="cli")
        with store.journal_path.open("a", encoding="utf-8") as journal:
            journal.write(
                'not json\n{"change": "moved"}\n{"change": "renamed", "at": 1, "pointer": {}}\n'
            )
        store.move("shadow", "imu:3", by="cli")

        await service.publish_pointers()

        assert _changes(graph) == [(MOVED, "active", 2, "cli"), (MOVED, "shadow", 3, "cli")]
        assert store.pointer_changes() == ([], b"")
        assert len(store.rejected_journal_path.read_bytes().splitlines()) == 3


@pytest.fixture(name="client")
async def client_fixture(
    db: WactorzDB, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(runtime, "db", db)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheHistoryRoute:
    async def test_it_lists_the_moves_newest_first(
        self, client: TestClient, graph: Provenance
    ) -> None:
        record_move(graph, _pointer(2, previous=1))
        record_move(graph, _pointer(3, previous=2))

        response = await client.get("/api/models/imu/moves")

        assert response.status == 200
        body = await response.json()
        assert body["name"] == "imu"
        assert [(m["version"], m["from"], m["by"], m["reason"]) for m in body["moves"]] == [
            (3, 2, "cli", "to 3"),
            (2, 1, "cli", "to 2"),
        ]

    async def test_a_limit_caps_it_and_a_bad_one_is_refused(
        self, client: TestClient, graph: Provenance
    ) -> None:
        record_move(graph, _pointer(2, previous=1))
        record_move(graph, _pointer(3, previous=2))

        capped = await (await client.get("/api/models/imu/moves", params={"limit": "1"})).json()
        assert [m["version"] for m in capped["moves"]] == [3]
        for bad in ("0", "lots", str(api_models.MAX_MOVES + 1)):
            assert (await client.get("/api/models/imu/moves", params={"limit": bad})).status == 400

    async def test_a_name_with_no_moves_has_an_empty_history(self, client: TestClient) -> None:
        body = await (await client.get("/api/models/imu/moves")).json()

        assert body == {"name": "imu", "moves": []}

    async def test_a_name_that_cannot_be_one_is_refused(self, client: TestClient) -> None:
        assert (await client.get("/api/models/-bad/moves")).status == 400

    async def test_without_persistence_it_says_so(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "db", None)
        monkeypatch.setattr(api_models, "get_db", lambda: None)

        response = await client.get("/api/models/imu/moves")

        assert response.status == 503
