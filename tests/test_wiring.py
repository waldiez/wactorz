# SPDX-License-Identifier: Apache-2.0
"""The wiring: who publishes what, who reads it, and where the data stops.

The graph is built from main's topic registry as it stands: agents and their
topics, what arrives from outside the system, what looks wrong, which models
an agent serves and where their pointers stand, and what each agent asked a
person. The route answers only where the registry lives, in a process with an
actor system.
"""

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import wactorz
from wactorz import config
from wactorz.agents.function_agent import spec_of
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.agents.shadow_serving import SHADOW_TOPIC_PREFIX
from wactorz.core import topic_bus
from wactorz.core.actor import Actor, ActorState, Message, declared_topics
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.paths import resolve_state_dir
from wactorz.core.topic_bus import OUTSIDE_TOPICS_KEPT, TopicBus, TopicContract, TopicRegistry
from wactorz.core.wiring import EXAMPLE_MAX_CHARS, SHADOW_ROOT, wiring
from wactorz.web import questions, runtime
from wactorz.web.app import build_app
from wactorz.web.wiring import NO_REGISTRY, pointers_of

KEY = "a-key-for-the-wiring-tests"
SCORER = str(uuid.UUID(int=1))
WATCH = str(uuid.UUID(int=2))
DRIFT = str(uuid.UUID(int=3))


def _no_pointers(_name: str) -> dict[str, int | None]:
    return {}


def _graph(
    *contracts: TopicContract, outside: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    kw.setdefault("pointers", _no_pointers)
    kw.setdefault("asked", {})
    return wiring(contracts, outside or {}, **kw)


def _topic(graph: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [t for t in graph["topics"] if t["topic"] == name]
    return found


def _kinds(graph: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted((w["kind"], w["topic"]) for w in graph["warnings"])


def scorer(**over: Any) -> TopicContract:
    fields: dict[str, Any] = {
        "name": "scorer",
        "publishes": ["anomalies/ahu-1"],
        "subscribes": ["sensors/ahu-1/#"],
        "actor_id": SCORER,
    }
    return TopicContract(**{**fields, **over})


def watch(**over: Any) -> TopicContract:
    fields: dict[str, Any] = {
        "name": "watch",
        "subscribes": ["anomalies/+"],
        "actor_id": WATCH,
    }
    return TopicContract(**{**fields, **over})


class TestTheGraph:
    def test_a_topic_joins_its_producer_to_its_readers(self) -> None:
        graph = _graph(scorer(), watch())

        topic = _topic(graph, "anomalies/ahu-1")
        assert (topic["producers"], topic["consumers"], topic["outside"]) == (
            [SCORER],
            [WATCH],
            False,
        )
        assert [a["id"] for a in graph["agents"]] == [SCORER, WATCH]
        assert graph["agents"][0]["node"] == "main"

    def test_an_agent_reading_its_own_topic_is_one_of_its_readers(self) -> None:
        loop = TopicContract(
            name="loop", publishes=["events/x"], subscribes=["events/#"], actor_id=WATCH
        )

        graph = _graph(loop)

        assert _topic(graph, "events/x")["consumers"] == [WATCH]
        assert _kinds(graph) == []

    def test_a_topic_nobody_reads_is_a_warning(self) -> None:
        assert ("unread", "anomalies/ahu-1") in _kinds(_graph(scorer()))

    def test_what_a_model_on_trial_publishes_is_marked_and_not_unread(self) -> None:
        trial = scorer()
        trial.update_observed("shadow/anomalies/ahu-1", {"score": 0.3})

        graph = _graph(trial, watch())

        assert _topic(graph, "shadow/anomalies/ahu-1")["shadow"] is True
        assert _topic(graph, "anomalies/ahu-1")["shadow"] is False
        assert ("unread", "shadow/anomalies/ahu-1") not in _kinds(graph)
        assert SHADOW_ROOT == SHADOW_TOPIC_PREFIX

    def test_a_subscription_under_a_world_root_is_an_outside_source_not_a_warning(self) -> None:
        graph = _graph(scorer(), watch())

        source = _topic(graph, "sensors/ahu-1/#")
        assert (source["outside"], source["consumers"], source["sampled"]) == (True, [SCORER], True)
        assert graph["sources"] == [{"root": "sensors", "topics": ["sensors/ahu-1/#"]}]
        assert _kinds(graph) == []

    def test_a_topic_main_does_not_sample_says_so(self) -> None:
        graph = _graph(watch(subscribes=["homeassistant/state_changes/#"]))

        source = _topic(graph, "homeassistant/state_changes/#")
        assert (source["outside"], source["sampled"], source["fields"]) == (True, False, None)

    def test_a_subscriber_with_no_producer_is_a_warning_listing_every_reader(self) -> None:
        graph = _graph(watch(subscribes=["drift/ahu-1"]), scorer(subscribes=["drift/ahu-1"]))

        (warning,) = [w for w in graph["warnings"] if w["kind"] == "no_producer"]
        assert (warning["topic"], sorted(warning["consumers"])) == (
            "drift/ahu-1",
            sorted([SCORER, WATCH]),
        )
        assert len([t for t in graph["topics"] if t["topic"] == "drift/ahu-1"]) == 1

    def test_the_systems_own_machinery_is_left_out(self) -> None:
        graph = _graph(
            scorer(publishes=["anomalies/ahu-1", f"agents/{SCORER}/heartbeat", "system/health"]),
            watch(subscribes=["anomalies/+", "nodes/+/heartbeat"]),
        )

        assert sorted(t["topic"] for t in graph["topics"]) == ["anomalies/ahu-1", "sensors/ahu-1/#"]

    def test_a_declared_pattern_is_shown_as_the_topics_seen_on_it(self) -> None:
        seen = scorer(publishes=["anomalies/+"])
        seen.update_observed("anomalies/ahu-1", {"score": 1.0})
        seen.update_observed("anomalies/ahu-2", {"score": 2.0})

        graph = _graph(seen, watch())

        published = sorted(t["topic"] for t in graph["topics"] if t["producers"])
        assert published == ["anomalies/ahu-1", "anomalies/ahu-2"]


class TestWhatATopicCarries:
    def test_the_last_payload_seen_with_its_fields_and_when(self) -> None:
        seen = scorer()
        seen.update_observed("anomalies/ahu-1", {"score": 0.4, "_trace": "x"})

        topic = _topic(_graph(seen, watch()), "anomalies/ahu-1")

        assert (topic["fields"], topic["fields_from"], topic["example"]) == (
            {"score": "float"},
            "seen",
            {"score": 0.4},
        )
        assert isinstance(topic["seen_at"], float)

    def test_before_anything_is_seen_the_producers_declaration(self) -> None:
        topic = _topic(
            _graph(scorer(produces_schema={"score": "float"}), watch()), "anomalies/ahu-1"
        )

        assert (topic["fields"], topic["fields_from"], topic["seen_at"]) == (
            {"score": "float"},
            "declared",
            None,
        )

    def test_a_long_payload_is_cut(self) -> None:
        seen = scorer()
        seen.update_observed("anomalies/ahu-1", {"blob": "x" * (EXAMPLE_MAX_CHARS * 2)})

        topic = _topic(_graph(seen, watch()), "anomalies/ahu-1")

        assert topic["example"] is None and topic["example_cut"] is True
        assert len(topic["example_text"]) == EXAMPLE_MAX_CHARS

    def test_a_topic_fed_from_outside_carries_what_was_seen_on_it(self) -> None:
        outside = TopicRegistry()
        outside.register(scorer())
        assert outside.record_outside("sensors/ahu-1/temp", {"temp": 17.4})

        graph = _graph(scorer(), watch(), outside=outside.outside_samples())

        topic = _topic(graph, "sensors/ahu-1/temp")
        assert (topic["outside"], topic["producers"], topic["fields"]) == (
            True,
            [],
            {"temp": "float"},
        )
        # The subscription it fills is not listed again as one with nothing behind it.
        assert all(t["topic"] != "sensors/ahu-1/#" for t in graph["topics"])


class TestFieldMismatches:
    def test_a_field_a_reader_declares_that_the_outside_topic_never_carries(self) -> None:
        drift = TopicContract(
            name="drift",
            subscribes=["sensors/ahu-1/#"],
            consumes_schema={"temperature": "float", "fan": "float"},
            actor_id=DRIFT,
        )
        outside = {
            "sensors/ahu-1/state": {
                "fields": {"temp": "float", "fan": "float"},
                "example": {},
                "at": 1.0,
            }
        }

        (warning,) = _graph(drift, outside=outside)["warnings"]

        assert warning == {
            "kind": "field_mismatch",
            "topic": "sensors/ahu-1/state",
            "consumer": DRIFT,
            "missing": ["temperature"],
            "carried": ["fan", "temp"],
            "carried_from": "seen",
        }

    def test_against_a_producers_declaration_before_anything_is_seen(self) -> None:
        graph = _graph(
            scorer(produces_schema={"score": "float"}),
            watch(consumes_schema={"anomaly_score": "float"}),
        )

        (warning,) = [w for w in graph["warnings"] if w["kind"] == "field_mismatch"]
        assert (warning["missing"], warning["carried_from"]) == (["anomaly_score"], "declared")

    def test_a_field_declared_as_any_names_nothing(self) -> None:
        graph = _graph(
            scorer(produces_schema={"score": "float"}),
            watch(consumes_schema={"trigger payload": "any"}),
        )

        assert all(w["kind"] != "field_mismatch" for w in graph["warnings"])
        assert graph["agents"][1]["reads_fields"] == []

    def test_a_topic_nothing_is_known_about_raises_none(self) -> None:
        graph = _graph(scorer(), watch(consumes_schema={"score": "float"}))

        assert all(w["kind"] != "field_mismatch" for w in graph["warnings"])


class TestWhatAnAgentServesAndAsks:
    def test_each_model_with_where_its_pointers_stand(self) -> None:
        def pointers(name: str) -> dict[str, int | None]:
            return {"active": 7, "shadow": 8} if name == "ahu-scorer" else {}

        graph = _graph(scorer(models={"model": "ahu-scorer"}), pointers=pointers)

        assert graph["agents"][0]["models"] == [
            {"key": "model", "name": "ahu-scorer", "active": 7, "shadow": 8}
        ]

    def test_what_it_asked_a_person(self) -> None:
        asked = {WATCH: {"open": 1, "answered": 2, "channels": ["dashboard", "telegram"]}}

        graph = _graph(scorer(), watch(), asked=asked)

        assert [a["asks"] for a in graph["agents"]] == [None, asked[WATCH]]


class TestTheOutsideTable:
    def test_only_a_topic_some_agent_reads_and_none_publishes_is_kept(self) -> None:
        registry = TopicRegistry()
        registry.register(scorer())

        # The scorer publishes it; nobody reads the second; the third is no payload.
        assert not registry.record_outside("anomalies/ahu-1", {"score": 1})
        assert not registry.record_outside("sensors/other/temp", {"t": 1})
        assert registry.record_outside("sensors/ahu-1/temp", {"t": 1})
        not_a_payload: Any = ["not", "a", "payload"]
        assert not registry.record_outside("sensors/ahu-1/temp", not_a_payload)

        assert list(registry.outside_samples()) == ["sensors/ahu-1/temp"]

    def test_it_keeps_the_topics_heard_from_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(topic_bus, "OUTSIDE_TOPICS_KEPT", 2)
        registry = TopicRegistry()
        registry.register(scorer())
        for topic in ("sensors/ahu-1/a", "sensors/ahu-1/b", "sensors/ahu-1/a", "sensors/ahu-1/c"):
            registry.record_outside(topic, {"v": 1})

        assert list(registry.outside_samples()) == ["sensors/ahu-1/a", "sensors/ahu-1/c"]
        assert OUTSIDE_TOPICS_KEPT > 2


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload
        self.properties = None


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained

    async def subscribe(self, _pattern: str, qos: int = 0, **_kwargs: Any) -> None:
        return None

    def __aiter__(self) -> "_Client":
        return self

    async def __anext__(self) -> _Message:
        if not self._messages:
            self._on_drained()
            raise StopAsyncIteration
        return self._messages.pop(0)

    @property
    def messages(self) -> "_Client":
        return self


class _Broker:
    """A stand-in for `mqtt_client`, which is an async context manager."""

    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.client = _Client(messages, on_drained)

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _main() -> MainActor:
    """A main with just what the manifests' sampling reads."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.migration = Migration(main, main.nodes)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    setattr(main, "_registry", None)
    setattr(main, "_get_spawn_registry", lambda: SpawnRegistry({}))
    return main


class TestMainSamplesTheOutside:
    async def test_a_payload_no_agent_published_is_kept_for_the_topic_an_agent_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        bus.register_contract(scorer())
        main = _main()

        def _stop() -> None:
            main.state = ActorState.STOPPED

        messages = [
            _Message("sensors/ahu-1/temp", json.dumps({"temp": 17.4}).encode()),
            _Message("sensors/nobody/temp", json.dumps({"temp": 1}).encode()),
            _Message("sensors/ahu-1/raw", b"not json"),
        ]
        monkeypatch.setattr("wactorz.agents.main.manifests.mqtt_client", _Broker(messages, _stop))
        monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: bus)

        await asyncio.wait_for(main.manifests._remote_observed_samples_listener(), timeout=5)

        kept = bus.registry.outside_samples()
        assert list(kept) == ["sensors/ahu-1/temp"]
        assert kept["sensors/ahu-1/temp"]["example"] == {"temp": 17.4}

    def test_a_manifests_models_reach_the_agents_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: bus)
        main = _main()

        main.manifests._register_contract(
            "scorer",
            {"name": "scorer", "actor_id": SCORER, "models": {"model": "ahu-scorer", "bad": 3}},
            ["anomalies/ahu-1"],
        )

        (contract,) = bus.registry.all_contracts()
        assert contract.models == {"model": "ahu-scorer"}


class TestAnAgentsManifest:
    async def test_it_names_the_models_the_agent_serves(self, tmp_path: Path) -> None:
        store = ArtifactStore.in_state_dir(Path(resolve_state_dir()))
        blob = tmp_path / "m.bin"
        blob.write_bytes(b"model")
        store.add(blob, "ahu-scorer", format_name="raw")

        @wactorz.agent(name="scorer", subscribes="sensors/ahu-1/#", models={"model": "ahu-scorer"})
        async def score(reading: dict[str, Any]) -> None:
            return None

        spec = spec_of(score)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        sent: list[tuple[str, Any]] = []

        async def publish(topic: str, payload: Any, **_: Any) -> None:
            sent.append((topic, payload))

        me._mqtt_publish = publish  # pyright: ignore[reportAttributeAccessIssue]
        await me._resolve_models()
        await me.publish_manifest(description="scores")

        ((topic, manifest),) = sent
        assert topic.endswith("/manifest") and manifest["models"] == {"model": "ahu-scorer"}

    async def test_an_agent_with_no_models_names_none(self, tmp_path: Path) -> None:
        @wactorz.agent(name="plain", subscribes="sensors/x")
        async def plain(reading: dict[str, Any]) -> None:
            return None

        spec = spec_of(plain)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        sent: list[Any] = []

        async def publish(_topic: str, payload: Any, **_: Any) -> None:
            sent.append(payload)

        me._mqtt_publish = publish  # pyright: ignore[reportAttributeAccessIssue]
        await me.publish_manifest()

        assert "models" not in sent[0]


class TestWhatAnAgentPublishes:
    async def test_a_payload_it_sends_is_kept_on_its_contract_beside_what_it_declared(
        self, tmp_path: Path, bus: TopicBus
    ) -> None:
        @wactorz.agent(name="scorer", subscribes="sensors/ahu-1/#", publishes="anomalies/ahu-1")
        async def score(reading: dict[str, Any]) -> None:
            return None

        spec = spec_of(score)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        bus.register_contract(
            TopicContract(name="scorer", publishes=["anomalies/ahu-1"], actor_id=me.actor_id)
        )

        async def publish(*_: Any, **__: Any) -> None:
            return None

        me._mqtt_publish = publish  # pyright: ignore[reportAttributeAccessIssue]
        await me.publish("scores/ahu-1", {"score": 0.4})
        await me.publish(f"agents/{me.actor_id}/logs", {"message": "hello"})
        await me.publish("scores/ahu-1/raw", b"bytes")

        contract = bus.registry.get("scorer", me.actor_id)
        assert contract is not None
        assert list(contract.observed_samples) == ["scores/ahu-1"]
        assert contract.publishes == ["anomalies/ahu-1"]

    async def test_without_main_in_the_process_nothing_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", None)

        @wactorz.agent(name="lonely", subscribes="sensors/x")
        async def lonely(reading: dict[str, Any]) -> None:
            return None

        spec = spec_of(lonely)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        sent: list[str] = []

        async def publish(topic: str, *_: Any, **__: Any) -> None:
            sent.append(topic)

        me._mqtt_publish = publish  # pyright: ignore[reportAttributeAccessIssue]
        await me.publish("scores/x", {"score": 1})

        assert sent == ["scores/x"]


class _Sent:
    """What an actor published, by topic."""

    def __init__(self) -> None:
        self.manifests: list[dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, *_: Any, **__: Any) -> None:
        if topic.endswith("/manifest") and isinstance(payload, dict):
            self.manifests.append(payload)


class _Declares(Actor):
    AGENT_NAME = "declares"
    DESCRIPTION = "Reads rooms, writes episodes."
    SUBSCRIBES = "air/+/score"
    PUBLISHES = ("air/episodes",)

    async def handle_message(self, msg: Message) -> None:
        return None


class _AnnouncesItself(_Declares):
    async def on_start(self) -> None:
        await self.publish_manifest(description="its own", publishes=["mine"])


class _Silent(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


async def _manifests_at_start(actor: Actor) -> list[dict[str, Any]]:
    sent = _Sent()
    actor._mqtt_publish = sent.publish  # pyright: ignore[reportAttributeAccessIssue]
    await actor.start()
    try:
        return sent.manifests
    finally:
        await actor.stop()


class TestAnActorClassAnnouncesWhatItDeclares:
    async def test_its_topics_and_description_go_out_as_its_manifest(self, tmp_path: Path) -> None:
        (manifest,) = await _manifests_at_start(
            _Declares(name="declares", persistence_dir=str(tmp_path))
        )

        assert (manifest["subscribes"], manifest["publishes"]) == (
            ["air/+/score"],
            ["air/episodes"],
        )
        assert manifest["description"] == "Reads rooms, writes episodes."

    async def test_one_that_announced_itself_keeps_its_own(self, tmp_path: Path) -> None:
        (manifest,) = await _manifests_at_start(
            _AnnouncesItself(name="own", persistence_dir=str(tmp_path))
        )

        assert (manifest["description"], manifest["publishes"]) == ("its own", ["mine"])

    async def test_one_that_declares_no_topics_announces_nothing(self, tmp_path: Path) -> None:
        assert (
            await _manifests_at_start(_Silent(name="silent", persistence_dir=str(tmp_path))) == []
        )

    @pytest.mark.parametrize(
        ("declared", "topics"),
        [("a/b", ("a/b",)), ("", ()), (("a", "", "b"), ("a", "b")), (None, ())],
    )
    def test_one_topic_or_several(self, declared: Any, topics: tuple[str, ...]) -> None:
        assert declared_topics(declared) == topics


class TestWhatEachAgentAsked:
    def test_open_and_answered_and_the_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(questions, "_books", {})
        questions.record(
            WATCH,
            {
                "agent": "watch",
                "questions": [
                    {
                        "id": "q1",
                        "text": "a?",
                        "options": ["y", "n"],
                        "channel": "dashboard",
                        "asked_at": 1.0,
                    },
                    {
                        "id": "q2",
                        "text": "b?",
                        "options": ["y", "n"],
                        "channel": "telegram",
                        "asked_at": 2.0,
                        "answer": "y",
                        "answered_at": 3.0,
                    },
                ],
            },
        )
        questions.record(SCORER, {"agent": "scorer", "questions": []})

        assert questions.asked_by() == {
            WATCH: {"open": 1, "answered": 1, "channels": ["dashboard", "telegram"]}
        }


class TestThePointers:
    def test_each_models_active_and_shadow_and_none_for_what_the_store_cannot_say(
        self, tmp_path: Path
    ) -> None:
        store = ArtifactStore.in_state_dir(Path(resolve_state_dir()))
        blob = tmp_path / "m.bin"
        for content in (b"one", b"two"):
            blob.write_bytes(content)
            store.add(blob, "ahu-scorer", format_name="raw")
        store.move("active", "ahu-scorer:1", by="test")
        store.move("shadow", "ahu-scorer:2", by="test")

        stands = pointers_of(["ahu-scorer", "unknown", "Not A Name!"])

        assert stands == {
            "ahu-scorer": {"active": 1, "shadow": 2},
            "unknown": {"active": None, "shadow": None},
            "Not A Name!": {"active": None, "shadow": None},
        }


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[TopicBus]:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    yield bus


@pytest.fixture(name="client")
async def client_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    monkeypatch.setattr(config, "CONFIG", dataclasses.replace(config.CONFIG, api_key=KEY))
    monkeypatch.setattr(runtime, "registry", None)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheRoute:
    async def test_it_answers_with_the_graph_where_the_registry_lives(
        self,
        client: TestClient[web.Request, web.Application],
        bus: TopicBus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "system", object())
        bus.register_contract(scorer(models={"model": "ahu-scorer"}))
        bus.register_contract(watch())

        response = await client.get("/api/wiring", headers={"X-API-Key": KEY})

        assert response.status == 200
        graph = await response.json()
        assert _topic(graph, "anomalies/ahu-1")["consumers"] == [WATCH]
        assert graph["agents"][0]["models"] == [
            {"key": "model", "name": "ahu-scorer", "active": None, "shadow": None}
        ]

    async def test_a_payload_json_cannot_say_is_said_as_text(
        self,
        client: TestClient[web.Request, web.Application],
        bus: TopicBus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "system", object())
        odd = scorer()
        odd.observed_samples["anomalies/ahu-1"] = {
            "fields": {},
            "example": {"at": {1, 2}},
            "at": 1.0,
        }
        bus.register_contract(odd)

        response = await client.get("/api/wiring", headers={"X-API-Key": KEY})

        assert response.status == 200

    async def test_without_an_actor_system_it_says_where_the_registry_lives(
        self,
        client: TestClient[web.Request, web.Application],
        bus: TopicBus,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "system", None)

        response = await client.get("/api/wiring", headers={"X-API-Key": KEY})

        assert response.status == 503
        assert (await response.json()) == {"error": NO_REGISTRY}

    async def test_it_needs_the_key(self, client: TestClient[web.Request, web.Application]) -> None:
        response = await client.get("/api/wiring")

        assert response.status == 401
