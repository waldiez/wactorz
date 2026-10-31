"""A model's shadow version runs beside the served one, and the trial is compared and recorded.

A function agent serving a model by its bare name takes the version the
``shadow`` pointer names, runs it after the active one on each message with a
view of the actor that reads the shadow version, publishes under ``shadow/``
and never writes the agent's state, and keeps one pair per message. A pass is
skipped, never queued, while the previous one runs. Main starts and ends the
trial in the serving agents when the pointer moves, compares the pairs when it
ends, and writes the outcome; the routes show the comparison and the outcomes.
``docs/design/model-pointers.md`` ("Shadow serving") is the decision these pin.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

import wactorz
from wactorz.agents import shadow_serving as agent_side
from wactorz.agents.function_agent import FunctionAgent, spec_of
from wactorz.agents.main import shadow_serving as main_side
from wactorz.agents.main.hosts import SwapHost
from wactorz.agents.main.shadow_serving import ShadowServing
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import ArtifactStore, pinned
from wactorz.core.labels import Labels
from wactorz.core.paths import resolve_state_dir
from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import OUTCOME, Provenance
from wactorz.core.shadow_trial import (
    CLEARED,
    PROMOTED,
    compare,
    outcomes_of,
    record_outcome,
    series,
)
from wactorz.data import data_root
from wactorz.data.parquet import Writer, frame
from wactorz.web import api_models, runtime
from wactorz.web.app import build_app

T0 = 1_760_000_000.0

# ── The store ─────────────────────────────────────────────────────────────────


@pytest.fixture(name="state")
def state_fixture() -> Path:
    """The state directory every test runs with (the conftest points it at a temp directory)."""
    return Path(resolve_state_dir())


@pytest.fixture(name="store")
def store_fixture(state: Path, tmp_path: Path) -> ArtifactStore:
    """``imu`` in two versions of raw bytes: v1 active, v2 on trial as shadow."""
    store = ArtifactStore.in_state_dir(state)
    for version in (1, 2):
        weights = tmp_path / f"imu-{version}.bin"
        weights.write_bytes(f"imu weights {version}".encode())
        store.add(weights, "imu", format_name="raw")
    store.move("active", "imu:1", by="test")
    store.move("shadow", "imu:2", by="dashboard", reason="trying it")
    return store


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "db" / "wactorz.db") as database:
        monkeypatch.setattr(main_side, "get_db", lambda: database)
        monkeypatch.setattr(api_models, "get_db", lambda: database)
        yield database


@pytest.fixture(name="graph")
def graph_fixture(db: WactorzDB) -> Provenance:
    return Provenance(db)


def pairs_of(state: Path, name: str = "imu") -> list[dict[str, Any]]:
    return frame(data_root(state), f"shadow/{name}", 0, 4_000_000_000).to_pylist()


# ── The function agent ────────────────────────────────────────────────────────


@pytest.fixture(name="published")
def published_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every MQTT publish any function agent makes, by topic."""
    seen: list[tuple[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        seen.append((topic, payload))

    monkeypatch.setattr(FunctionAgent, "_mqtt_publish", record)
    return seen


@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    models={"model": "imu"},
)
async def detect(reading: dict[str, Any], me: wactorz.FunctionAgent) -> dict[str, Any] | None:
    """Score a reading with whatever version ``me`` holds; remember the version that saw it."""
    weights = await me.load_model("model")
    version = int(weights.decode().split()[-1])
    me.persist("last_version", version)
    if version == 2 and reading.get("slow"):
        await asyncio.sleep(0.05)
    score = float(reading["x"]) * version
    if score < 0:
        return None
    await me.publish("custom/scores", {"score": score})
    return {"score": score, "version": version}


def _topics(published: list[tuple[str, Any]], topic: str) -> list[Any]:
    return [payload for t, payload in published if t == topic]


async def _settled(agent: FunctionAgent) -> None:
    """Wait for the shadow pass the last message started, if any."""
    for _ in range(200):
        if not agent._shadow_busy and not any(
            t.get_name() == f"{agent.name}:shadow" and not t.done() for t in agent._tasks
        ):
            return
        await asyncio.sleep(0.005)
    pytest.fail("the shadow pass never finished")


@pytest.fixture(name="agent")
async def agent_fixture(
    store: ArtifactStore, tmp_path: Path, published: list[tuple[str, Any]]
) -> AsyncIterator[FunctionAgent]:
    spec = spec_of(detect)
    assert spec is not None
    built = spec.build(persistence_dir=str(tmp_path / "agents"))
    built.state = ActorState.RUNNING
    await built._resolve_models()
    await built._resolve_shadows()
    yield built
    await built.on_stop()
    for task in built._tasks:
        task.cancel()


class TestTheShadowPass:
    async def test_the_shadow_version_answers_after_the_active_one(
        self, agent: FunctionAgent, published: list[tuple[str, Any]], state: Path
    ) -> None:
        assert agent.shadow["model"]["version"] == 2

        await agent._on_message({"x": 3, "ts": T0}, "sensors/imu/#")
        await _settled(agent)

        assert _topics(published, "anomalies/imu") == [{"score": 3.0, "version": 1}]
        assert _topics(published, "shadow/anomalies/imu") == [{"score": 6.0, "version": 2}]
        assert _topics(published, "custom/scores") == [{"score": 3.0}]
        assert _topics(published, "shadow/custom/scores") == [{"score": 6.0}]
        assert agent.shadow_passes == 1

    async def test_the_trial_never_writes_the_agents_memory(self, agent: FunctionAgent) -> None:
        await agent._on_message({"x": 3, "ts": T0}, "sensors/imu/#")
        await _settled(agent)

        assert agent.recall("last_version") == 1

    async def test_one_pair_per_message_is_kept_in_event_time(
        self, agent: FunctionAgent, state: Path
    ) -> None:
        await agent._on_message({"x": 3, "ts": T0, "device": "left-wrist"}, "sensors/imu/#")
        await _settled(agent)
        await agent._on_message({"x": -1, "ts": T0 + 1}, "sensors/imu/#")
        await _settled(agent)
        await agent._pairs["imu"].flush()

        rows = pairs_of(state)

        assert [(r["ts"], r["source"]) for r in rows] == [
            (T0, "left-wrist"),
            (T0 + 1, "sensors/imu/#"),
        ]
        assert (rows[0]["active_version"], rows[0]["shadow_version"]) == (1.0, 2.0)
        assert json.loads(rows[0]["active_output"]) == {"score": 3.0, "version": 1}
        assert json.loads(rows[0]["shadow_output"]) == {"score": 6.0, "version": 2}
        assert rows[1]["active_output"] is None and rows[1]["shadow_output"] is None
        assert rows[0]["active_ms"] >= 0 and rows[0]["shadow_ms"] >= 0
        assert rows[0]["time_source"] == "payload"

    async def test_a_pass_is_skipped_not_queued_while_the_last_one_runs(
        self, agent: FunctionAgent, published: list[tuple[str, Any]]
    ) -> None:
        await agent._on_message({"x": 1, "slow": True}, "sensors/imu/#")
        await agent._on_message({"x": 2}, "sensors/imu/#")
        await _settled(agent)

        assert len(_topics(published, "anomalies/imu")) == 2
        assert len(_topics(published, "shadow/anomalies/imu")) == 1
        assert (agent.shadow_passes, agent.shadow_skipped) == (1, 1)

    async def test_clearing_the_shadow_ends_the_trial(
        self, agent: FunctionAgent, published: list[tuple[str, Any]]
    ) -> None:
        await agent.swap_shadow("model", None)
        await agent._on_message({"x": 3}, "sensors/imu/#")
        await _settled(agent)

        assert agent.shadow == {}
        assert _topics(published, "shadow/anomalies/imu") == []
        assert agent._pairs == {}
        assert any("no longer trying imu:2" in p.get("message", "") for _, p in published)

    async def test_a_shadow_that_will_not_load_leaves_the_trial_as_it_was(
        self, agent: FunctionAgent, store: ArtifactStore
    ) -> None:
        await agent.load_model("model")
        gone = {**pinned(store.resolve("imu:2")), "path": str(store.root / "nowhere.bin")}

        with pytest.raises(FileNotFoundError):
            await agent.swap_shadow("model", gone)

        assert agent.shadow["model"]["path"] != gone["path"]

    async def test_without_a_shadow_pointer_there_is_no_pass(
        self, store: ArtifactStore, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        store.clear("shadow", "imu", by="test")
        spec = spec_of(detect)
        assert spec is not None
        agent = spec.build(persistence_dir=str(tmp_path / "agents"))
        await agent._resolve_models()
        await agent._resolve_shadows()

        await agent._on_message({"x": 3}, "sensors/imu/#")

        assert agent.shadow == {}
        assert _topics(published, "shadow/anomalies/imu") == []

    async def test_an_agent_on_a_node_runs_no_shadow_yet(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        spec = spec_of(detect)
        assert spec is not None
        agent = spec.build(persistence_dir=str(tmp_path / "agents"))
        agent._node = "rpi"
        await agent._resolve_models()

        await agent._resolve_shadows()

        assert agent.shadow == {}

    async def test_a_failing_shadow_pass_is_a_pair_with_the_error(
        self, store: ArtifactStore, tmp_path: Path, published: list[tuple[str, Any]], state: Path
    ) -> None:
        @wactorz.agent(name="fragile", subscribes="s/#", publishes="out", models={"model": "imu"})
        async def fragile(reading: dict[str, Any], me: wactorz.FunctionAgent) -> dict[str, Any]:
            if me.artifacts["model"]["version"] == 2:
                raise RuntimeError("v2 cannot read this")
            return {"ok": True}

        spec = spec_of(fragile)
        assert spec is not None
        agent = spec.build(persistence_dir=str(tmp_path / "agents"))
        agent.state = ActorState.RUNNING
        await agent._resolve_models()
        await agent._resolve_shadows()
        try:
            await agent._on_message({"x": 1, "ts": T0}, "s/#")
            await _settled(agent)
            await agent._pairs["imu"].flush()
        finally:
            await agent.on_stop()

        (row,) = pairs_of(state)
        assert row["shadow_error"] == "RuntimeError: v2 cannot read this"
        assert row["shadow_output"] is None
        assert _topics(published, "out") == [{"ok": True}]


class TestWhatTheShadowSees:
    async def test_the_view_reads_the_shadow_and_reaches_the_actor_for_the_rest(
        self, agent: FunctionAgent, store: ArtifactStore
    ) -> None:
        view = agent_side.ShadowView(agent)

        assert view.artifacts["model"]["version"] == 2
        assert view.artifact_path("model") == agent.shadow["model"]["path"]
        assert await view.load_model("model") == b"imu weights 2"
        assert await agent.load_model("model") == b"imu weights 1"
        assert view.name == agent.name
        assert view.options is agent.options

    def test_an_input_hashes_the_same_whatever_its_key_order(self) -> None:
        assert agent_side.input_sha256({"a": 1, "b": 2}) == agent_side.input_sha256(
            {"b": 2, "a": 1}
        )


# ── Main's side ───────────────────────────────────────────────────────────────


class _Serving:
    """A function agent on main, as the trial reaches it."""

    def __init__(self, name: str, entry: dict[str, Any]) -> None:
        self.name = name
        self.artifacts = {"model": dict(entry)}
        self.swaps: list[tuple[str, dict[str, Any] | None]] = []

    async def swap_shadow(self, key: str, entry: dict[str, Any] | None) -> None:
        self.swaps.append((key, None if entry is None else dict(entry)))


class _Plain:
    """An agent with models but no shadow of its own: a dynamic agent."""

    name = "plain"

    def __init__(self, entry: dict[str, Any]) -> None:
        self.artifacts = {"model": dict(entry)}


class _Registry:
    def __init__(self) -> None:
        self.running: list[Any] = []

    def all_actors(self) -> list[Any]:
        return list(self.running)

    def find_by_name(self, name: str) -> Any:
        return next((a for a in self.running if a.name == name), None)


class _Host:
    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self._registry = _Registry()
        self.published: list[tuple[str, Any]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names({})

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        pass

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        pass

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    def feed(self) -> list[str]:
        return [
            p["message"]
            for t, p in self.published
            if t == "agents/main-id/logs" and p.get("type") == "model"
        ]


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


@pytest.fixture(name="serving")
def serving_fixture(host: _Host, store: ArtifactStore) -> _Serving:
    actor = _Serving("imu-anomaly", {**pinned(store.resolve("imu:1")), "follows": "active"})
    host._registry.running.append(actor)
    host._registry.running.append(_Plain({**pinned(store.resolve("imu:1")), "follows": "active"}))
    return actor


@pytest.fixture(name="later")
def later_fixture(store: ArtifactStore, monkeypatch: pytest.MonkeyPatch) -> float:
    """``now`` as main and the routes read it: well after the trial's pairs, which are seconds apart."""
    shadow = store.pointer("imu", "shadow")
    assert shadow is not None
    now = shadow.moved_at + 100
    monkeypatch.setattr(main_side, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(api_models, "time", SimpleNamespace(time=lambda: now))
    return now


@pytest.fixture(name="trials")
def trials_fixture(host: _Host) -> ShadowServing:
    # The fake declares what the service reads; it stands in for MainActor.
    return ShadowServing(cast("SwapHost", host))


def _pairs(state: Path, since: float, count: int) -> None:
    """``count`` pairs of ``imu``'s trial from ``since``, agreeing on all but the last."""
    writer = Writer(data_root(state), "shadow/imu")
    for i in range(count):
        same = i < count - 1
        writer.append(
            {
                "ts": since + i,
                "arrived": since + i,
                "time_source": "payload",
                "input_sha256": "a" * 64,
                "source": "left-wrist",
                "active_version": 1,
                "shadow_version": 2,
                "active_output": json.dumps({"score": 1.0}),
                "shadow_output": json.dumps({"score": 1.0 if same else 3.0}),
                "active_ms": 1.0,
                "shadow_ms": 2.0,
                "shadow_error": None,
            }
        )
    writer.flush()


class TestMainStartsAndEndsTheTrial:
    async def test_a_set_pointer_gives_the_serving_agents_the_version(
        self, host: _Host, serving: _Serving, trials: ShadowServing, store: ArtifactStore
    ) -> None:
        now = store.pointer("imu", "shadow")

        await trials.follow([("imu", "shadow", now, None)])

        ((key, entry),) = serving.swaps
        assert key == "model"
        assert entry is not None
        assert (entry["version"], entry["follows"]) == (2, "shadow")
        assert Path(entry["path"]).read_bytes() == b"imu weights 2"

    async def test_the_active_pointer_moving_is_not_the_trials_business(
        self, serving: _Serving, trials: ShadowServing, store: ArtifactStore
    ) -> None:
        await trials.follow([("imu", "active", store.pointer("imu", "active"), None)])

        assert serving.swaps == []

    async def test_a_cleared_pointer_ends_the_trial_and_records_what_it_showed(
        self,
        later: float,
        host: _Host,
        serving: _Serving,
        trials: ShadowServing,
        store: ArtifactStore,
        graph: Provenance,
        state: Path,
    ) -> None:
        was = store.pointer("imu", "shadow")
        assert was is not None
        _pairs(state, was.moved_at, 4)
        store.clear("shadow", "imu", by="dashboard")

        await trials.follow([("imu", "shadow", None, was)])

        assert serving.swaps == [("model", None)]
        assert host.feed() == ["the trial of imu:2 ended (cleared): 4 pair(s), agreed on 75%"]
        (outcome,) = outcomes_of(graph, "imu")
        assert (outcome["ended"], outcome["shadow_version"], outcome["active_version"]) == (
            CLEARED,
            2,
            1,
        )
        assert (outcome["pairs"], outcome["agreement"]) == (4, 0.75)
        assert (outcome["since"], outcome["until"]) == (was.moved_at, later)
        assert outcome["score"]["difference_mean"] == 0.5

    async def test_a_promotion_ends_the_trial_as_won(
        self,
        later: float,
        host: _Host,
        serving: _Serving,
        trials: ShadowServing,
        store: ArtifactStore,
        graph: Provenance,
        state: Path,
    ) -> None:
        was = store.pointer("imu", "shadow")
        assert was is not None
        _pairs(state, was.moved_at, 2)
        store.promote("imu", by="dashboard", reason="better")

        await trials.follow([("imu", "shadow", None, was)])

        (outcome,) = outcomes_of(graph, "imu")
        assert (outcome["ended"], outcome["active_version"], outcome["by"]) == (
            PROMOTED,
            1,
            "dashboard",
        )

    async def test_the_same_ending_heard_twice_is_one_outcome(
        self, serving: _Serving, trials: ShadowServing, store: ArtifactStore, graph: Provenance
    ) -> None:
        was = store.pointer("imu", "shadow")
        store.clear("shadow", "imu", by="dashboard")

        await trials.follow([("imu", "shadow", None, was)])
        await trials.follow([("imu", "shadow", None, was)])

        assert len(graph.nodes(OUTCOME)) == 1

    async def test_a_version_that_cannot_be_pinned_is_said(
        self, host: _Host, serving: _Serving, trials: ShadowServing, store: ArtifactStore
    ) -> None:
        now = store.pointer("imu", "shadow")
        store.clear("shadow", "imu", by="test")

        await trials.follow([("imu", "shadow", now, None)])

        assert serving.swaps == []
        assert host.feed() == [f"imu:shadow cannot be tried: {_no_shadow(store)}"]


def _no_shadow(store: ArtifactStore) -> str:
    try:
        store.pin("imu:shadow")
    except Exception as exc:
        return str(exc)
    return ""


# ── The comparison ────────────────────────────────────────────────────────────


def _row(ts: float, active: Any, shadow: Any, **extra: Any) -> dict[str, Any]:
    return {
        "ts": ts,
        "active_output": None if active is None else json.dumps(active),
        "shadow_output": None if shadow is None else json.dumps(shadow),
        "active_ms": 1.0,
        "shadow_ms": 3.0,
        "active_version": 1,
        "shadow_version": 2,
        **extra,
    }


class TestTheSeries:
    def test_a_row_at_the_end_of_the_window_is_outside_it(self) -> None:
        kept = series([_row(10.0, 1, 1)], since=0.0, until=10.0, points=2)

        assert [bucket["pairs"] for bucket in kept] == [0, 0]

    def test_each_stretch_says_its_middle(self) -> None:
        kept = series([], since=0.0, until=10.0, points=2)

        assert [bucket["ts"] for bucket in kept] == [2.5, 7.5]

    @pytest.mark.parametrize(("until", "points"), [(0.0, 2), (-1.0, 2), (10.0, 0)])
    def test_an_empty_window_has_no_stretches(self, until: float, points: int) -> None:
        assert series([], since=0.0, until=until, points=points) == []

    def test_a_flag_is_not_a_latency(self) -> None:
        (bucket,) = series([_row(1.0, 1, 1, active_ms=True)], since=0.0, until=10.0, points=1)

        assert bucket["active_ms"] is None


class TestTheComparison:
    def test_agreement_scores_and_latency(self) -> None:
        rows = [
            _row(T0, {"score": 1.0}, {"score": 1.0}),
            _row(T0 + 1, None, None),
            _row(T0 + 2, {"score": 2.0}, {"score": 4.0}),
            _row(T0 + 3, None, {"score": 5.0}, shadow_error=None),
            _row(T0 + 4, {"score": 1.0}, None, shadow_error="RuntimeError: x"),
        ]

        summary = compare(rows)

        assert summary["pairs"] == 5
        assert summary["agreement"] == 0.4
        assert summary["flagged"] == {"active": 3, "shadow": 3}
        assert summary["shadow_errors"] == 1
        assert summary["score"]["difference_mean"] == 1.0
        assert summary["score"]["scored"] == 2
        assert summary["latency"]["shadow"]["mean_ms"] == 3.0
        assert summary["versions"] == {"active": [1], "shadow": [2]}
        assert compare([])["agreement"] is None

    def test_precision_and_recall_against_the_labels(self, db: WactorzDB) -> None:
        labels = Labels(db)
        labels.write(source="left-wrist", start=T0, end=T0 + 2, value="fault", actor="op")
        labels.write(source="left-wrist", start=T0 + 2, end=T0 + 4, value="maintenance", actor="op")
        rows = [
            _row(T0, {"score": 9}, {"score": 9}),  # fault: both right
            _row(T0 + 1, None, {"score": 9}),  # fault: shadow right, active missed
            _row(T0 + 2, {"score": 9}, None),  # maintenance: active wrong, shadow right
            _row(T0 + 3, None, None),  # maintenance: both right
            _row(T0 + 10, {"score": 9}, None),  # unlabelled: left out
        ]

        summary = compare(rows, intervals=labels.effective("left-wrist"), positive=["fault"])

        against = summary["labels"]
        assert (against["labelled"], against["positives"]) == (4, 2)
        assert (against["active"]["precision"], against["active"]["recall"]) == (0.5, 0.5)
        assert (against["shadow"]["precision"], against["shadow"]["recall"]) == (1.0, 1.0)

    def test_an_outcome_is_about_the_model_and_written_once(self, graph: Provenance) -> None:
        summary = compare([_row(T0, {"score": 1}, {"score": 1})])
        for _ in range(2):
            record_outcome(
                graph,
                name="imu",
                shadow_version=2,
                active_version=1,
                since=T0,
                until=T0 + 60,
                ended=CLEARED,
                summary=summary,
                by="",
            )

        (outcome,) = outcomes_of(graph, "imu")
        assert (outcome["pairs"], outcome["until"], outcome["what"]) == (1, T0 + 60, "shadow_trial")


# ── The routes ────────────────────────────────────────────────────────────────


@pytest.fixture(name="client")
async def client_fixture(
    db: WactorzDB, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(runtime, "db", db)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheTrialRoutes:
    async def test_the_trial_so_far_is_compared_from_when_the_shadow_was_set(
        self, client: TestClient, later: float, store: ArtifactStore, state: Path
    ) -> None:
        shadow = store.pointer("imu", "shadow")
        assert shadow is not None
        _pairs(state, shadow.moved_at, 3)

        body = await (await client.get("/api/models/imu/trial")).json()

        assert (body["pairs"], body["since"], body["shadow"]["version"]) == (3, shadow.moved_at, 2)
        assert body["agreement"] == pytest.approx(2 / 3)
        assert "labels" not in body

    async def test_labels_are_joined_when_a_source_and_its_positive_values_are_named(
        self, client: TestClient, later: float, store: ArtifactStore, state: Path, db: WactorzDB
    ) -> None:
        shadow = store.pointer("imu", "shadow")
        assert shadow is not None
        _pairs(state, shadow.moved_at, 3)
        Labels(db).write(source="left-wrist", start=shadow.moved_at, value="fault", actor="op")

        body = await (
            await client.get(
                "/api/models/imu/trial", params={"source": "left-wrist", "positive": "fault"}
            )
        ).json()

        assert body["labels"]["positives"] == 3
        assert body["labels"]["active"]["recall"] == 1.0

    async def test_the_series_cuts_the_trial_into_stretches_of_time(
        self, client: Any, state: Path
    ) -> None:
        writer = Writer(data_root(state), "shadow/imu")
        for offset, active, shadow, ms in (
            (1, {"score": 1.0}, {"score": 3.0}, 2.0),
            (2, {"score": 2.0}, {"score": 4.0}, 4.0),
            (31, {"score": 5.0}, None, 1.0),
        ):
            writer.append(
                {
                    **_row(T0 + offset, active, shadow, active_ms=ms, shadow_ms=ms + 2),
                    "arrived": T0 + offset,
                    "time_source": "payload",
                }
            )
        writer.flush()

        answer = await client.get(
            "/api/models/imu/trial/series",
            params={"since": T0, "until": T0 + 40, "points": 4},
        )

        assert answer.status == 200
        first, empty, _third, last = (await answer.json())["buckets"]
        assert (first["pairs"], first["active_score"], first["shadow_score"]) == (2, 1.5, 3.5)
        assert (first["active_ms"], first["shadow_ms"]) == (3.0, 5.0)
        assert (empty["pairs"], empty["active_score"], empty["shadow_ms"]) == (0, None, None)
        assert (last["active_score"], last["shadow_score"]) == (5.0, None)

    @pytest.mark.parametrize("points", ["0", "-1", "many", "1001"])
    async def test_the_series_refuses_a_bad_count(self, client: Any, points: str) -> None:
        answer = await client.get("/api/models/imu/trial/series", params={"points": points})

        assert answer.status == 400

    async def test_without_a_shadow_there_is_no_trial_unless_a_window_is_given(
        self, client: TestClient, later: float, store: ArtifactStore, state: Path
    ) -> None:
        shadow = store.pointer("imu", "shadow")
        assert shadow is not None
        _pairs(state, shadow.moved_at, 2)
        store.clear("shadow", "imu", by="test")

        assert (await client.get("/api/models/imu/trial")).status == 404
        assert (await client.get("/api/models/imu/trial/series")).status == 404
        body = await (
            await client.get("/api/models/imu/trial", params={"since": shadow.moved_at})
        ).json()
        assert body["pairs"] == 2
        assert (await client.get("/api/models/imu/trial", params={"since": "soon"})).status == 400

    async def test_the_outcomes_of_ended_trials_are_listed(
        self, client: TestClient, graph: Provenance
    ) -> None:
        record_outcome(
            graph,
            name="imu",
            shadow_version=2,
            active_version=1,
            since=T0,
            until=T0 + 60,
            ended=PROMOTED,
            summary=compare([]),
            by="dashboard",
        )

        body = await (await client.get("/api/models/imu/trials")).json()

        assert [(t["shadow_version"], t["ended"]) for t in body["trials"]] == [(2, PROMOTED)]
