"""Resolving a vague reference to a concrete topic or Home Assistant entity.

A task says "temperature"; a spawned agent needs a topic name and the field
inside its payload. These cover the resolution path end to end, against a fake
TopicBus and a fake Home Assistant agent.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner.agent import PlannerAgent

BUS_DOWN = "bus is down"


class FakeContract:
    def __init__(self, name: str, publishes: list[str], node: str = "") -> None:
        self.name = name
        self.publishes = publishes
        self.node = node
        self.produces_schema: dict[str, Any] = {}


class FakeTopicRegistry:
    def __init__(self, contracts: list[FakeContract]) -> None:
        self._contracts = contracts

    def find_by_capability(self, keyword: str) -> list[FakeContract]:
        return [c for c in self._contracts if keyword in " ".join(c.publishes)]

    def all_contracts(self) -> list[FakeContract]:
        return list(self._contracts)


class FakeBus:
    def __init__(self, *contracts: FakeContract) -> None:
        self.registry = FakeTopicRegistry(list(contracts))


def make_planner(tmp_path: Path) -> PlannerAgent:
    return PlannerAgent(
        llm_provider=FakeProvider(), persistence_dir=str(tmp_path), auto_terminate=False
    )


def use_bus(monkeypatch: pytest.MonkeyPatch, bus: FakeBus | None) -> None:
    """Point the topic-bus lookup at a stand-in.

    `_resolve_data_references` imports `get_topic_bus` inside the function to
    break a circular import, so the patch has to land on the source module
    rather than on a name in `context`.
    """
    import wactorz.core.topic_bus as topic_bus

    monkeypatch.setattr(topic_bus, "get_topic_bus", lambda: bus)


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path) -> PlannerAgent:
    return make_planner(tmp_path)


class TestResolveDataReferences:
    async def test_a_task_naming_no_data_concept_is_left_alone(self, planner: PlannerAgent) -> None:
        """Nothing vague to resolve, so nothing is added to the task."""
        task, note = await planner._resolve_data_references("tell me a joke")

        assert task == "tell me a joke"
        assert not note

    async def test_a_single_matching_topic_is_resolved_outright(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_bus(monkeypatch, FakeBus(FakeContract("sensor-agent", ["home/temperature"])))

        task, note = await planner._resolve_data_references("alert me when temperature is high")

        assert "home/temperature" in task
        assert "agent.subscribe" in task, "the agent is told how to consume it"
        assert "sensor-agent" in note

    async def test_several_matches_are_all_offered_to_the_model(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Picking by keyword count would guess; the model knows the intent."""
        use_bus(
            monkeypatch,
            FakeBus(
                FakeContract("indoor", ["home/temperature/indoor"]),
                FakeContract("outdoor", ["home/temperature/outdoor"]),
            ),
        )

        task, note = await planner._resolve_data_references("watch the temperature")

        assert "MULTIPLE DATA SOURCES" in task
        assert "home/temperature/indoor" in task
        assert "home/temperature/outdoor" in task
        assert "2 matching topics" in note

    async def test_the_publishing_node_is_named_when_there_is_one(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A topic on another node needs the node said out loud."""
        use_bus(monkeypatch, FakeBus(FakeContract("sensor", ["home/temperature"], node="pi-2")))

        task, note = await planner._resolve_data_references("alert me on temperature")

        assert "on pi-2" in task
        assert "pi-2" in note

    async def test_no_topic_and_no_home_assistant_says_what_was_looked_for(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_bus(monkeypatch, FakeBus())

        task, note = await planner._resolve_data_references("alert me when temperature is high")

        assert "No registered MQTT topics or HA entities found" in task
        assert note == "" or "temperature" in note

    async def test_a_broken_topic_bus_does_not_stop_planning(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution is an enrichment; failing it must not fail the request."""

        class Exploding:
            @property
            def registry(self) -> Any:
                raise RuntimeError(BUS_DOWN)

        use_bus(monkeypatch, Exploding())  # pyright: ignore[reportArgumentType]

        task, _note = await planner._resolve_data_references("alert me when temperature is high")

        assert task.startswith("alert me when temperature is high")


class TestHomeAssistantFallback:
    """When no agent publishes a topic, HA entities are the next best source."""

    @staticmethod
    def _entities(planner: PlannerAgent, entities: list[dict[str, Any]]) -> None:
        async def _fetch() -> list[dict[str, Any]]:
            return entities

        planner._fetch_ha_entities = _fetch  # pyright: ignore[reportAttributeAccessIssue]

    async def test_a_single_matching_entity_is_resolved(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_bus(monkeypatch, FakeBus())
        self._entities(
            planner,
            [{"entity_id": "sensor.hall_temperature", "name": "Hall", "state": "21"}],
        )

        task, note = await planner._resolve_data_references("alert me when temperature is high")

        assert "sensor.hall_temperature" in task
        assert "homeassistant/state_changes/#" in task
        assert "Hall" in note

    async def test_the_legacy_nested_device_shape_still_resolves(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A node on an older build answers with devices wrapping entities."""
        use_bus(monkeypatch, FakeBus())
        self._entities(
            planner,
            [{"entities": [{"entity_id": "sensor.temp_probe", "name": "Probe", "state": "9"}]}],
        )

        task, _note = await planner._resolve_data_references("alert me when temperature is high")

        assert "sensor.temp_probe" in task

    async def test_several_entities_are_all_offered(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_bus(monkeypatch, FakeBus())
        self._entities(
            planner,
            [
                {"entity_id": "sensor.a_temperature", "name": "A", "state": "1"},
                {"entity_id": "sensor.b_temperature", "name": "B", "state": "2"},
            ],
        )

        task, note = await planner._resolve_data_references("alert me when temperature is high")

        assert "MULTIPLE HA ENTITIES FOUND" in task
        assert "2 HA entities" in note

    async def test_entities_that_match_nothing_are_not_offered(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_bus(monkeypatch, FakeBus())
        self._entities(planner, [{"entity_id": "light.kitchen", "name": "Kitchen", "state": "on"}])

        task, _note = await planner._resolve_data_references("alert me when temperature is high")

        assert "No registered MQTT topics or HA entities found" in task
