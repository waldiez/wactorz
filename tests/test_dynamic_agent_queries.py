"""Reading back what the system has recorded, from inside generated code.

Every query is synchronous and returns a list, so generated code can iterate
the result without awaiting it; with `as_dataframe=True` it returns a pandas
frame when pandas is installed and the same list when it is not. Before
persistence is initialised there is nothing to read, which is an empty list
rather than an error.
"""

import builtins
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core import topic_bus
from wactorz.core.persistence import PickleStore, WactorzDB
from wactorz.core.persistence.stores import install_stores
from wactorz.core.topic_bus import TopicBus


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path) -> AgentAPI:
    return AgentAPI(DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path)))


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    install_stores(db, PickleStore(str(tmp_path / "pickles")))
    now = time.time()
    db.write_sensor(now, "sensors/t", "sensor.kitchen", "temp", 21.5)
    db.write_sensor(now - 48 * 3600, "sensors/t", "sensor.kitchen", "temp", 18.0)
    db.write_detection(now, "camera", "person", 0.91)
    db.write_detection(now, "camera", "cat", 0.4)
    db.write_ha_state(now, "light.hall", "off", "on", domain="light")
    yield db


@pytest.fixture(name="no_pandas")
def no_pandas_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pandas":
            raise ImportError("no pandas here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)


class TestWithoutPersistence:
    def test_every_query_is_empty(self, api: AgentAPI) -> None:
        assert api.query_ts() == []
        assert api.query_detections() == []
        assert api.query_ha_states() == []
        assert api.ts_stats() == {}

    def test_a_dataframe_request_is_none(self, api: AgentAPI) -> None:
        assert api.query_ts(as_dataframe=True) is None
        assert api.query_detections(as_dataframe=True) is None
        assert api.query_ha_states(as_dataframe=True) is None


class TestQueries:
    def test_readings_are_filtered_by_age_and_field(self, api: AgentAPI, db: WactorzDB) -> None:
        rows = api.query_ts(hours=24, field="temp")

        assert [row["value"] for row in rows] == [21.5]

    def test_detections_are_filtered_by_class_and_confidence(
        self, api: AgentAPI, db: WactorzDB
    ) -> None:
        assert [r["class_name"] for r in api.query_detections(min_confidence=0.5)] == ["person"]
        assert [r["class_name"] for r in api.query_detections(class_name="cat")] == ["cat"]

    def test_ha_states_are_filtered_by_domain(self, api: AgentAPI, db: WactorzDB) -> None:
        (row,) = api.query_ha_states(domain="light")

        assert (row["entity_id"], row["new_state"]) == ("light.hall", "on")

    def test_stats_count_the_tables(self, api: AgentAPI, db: WactorzDB) -> None:
        stats = api.ts_stats()

        assert stats["sensor_readings"] == 2
        assert stats["detections"] == 2

    def test_dataframes_when_pandas_is_installed(self, api: AgentAPI, db: WactorzDB) -> None:
        pd = pytest.importorskip("pandas")

        for frame in (
            api.query_ts(as_dataframe=True),
            api.query_detections(as_dataframe=True),
            api.query_ha_states(as_dataframe=True),
        ):
            assert isinstance(frame, pd.DataFrame)

    def test_lists_when_pandas_is_missing(
        self, api: AgentAPI, db: WactorzDB, no_pandas: None
    ) -> None:
        assert isinstance(api.query_ts(as_dataframe=True), list)
        assert isinstance(api.query_detections(as_dataframe=True), list)
        assert isinstance(api.query_ha_states(as_dataframe=True), list)


class TestWorldState:
    async def test_without_a_bus_it_is_published_on_the_agents_data_topic(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", None)
        published: list[tuple[str, Any]] = []

        async def _publish(topic: str, data: Any) -> None:
            published.append((topic, data))

        monkeypatch.setattr(api, "publish", _publish)

        await api.publish_world_state("presence", {"present": True})

        assert published == [("agents/probe/data/presence", {"present": True})]

    async def test_with_a_bus_the_state_hub_publishes_it(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)

        await api.publish_world_state("energy", {"kwh": 2.3})

        assert bus.state_hub._cache["agents/probe/data/energy"] == {"kwh": 2.3}

    async def test_reading_waits_for_the_retained_message(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[str, float]] = []

        async def _get(topic: str, timeout: float = 10.0) -> Any:
            asked.append((topic, timeout))
            return {"present": False}

        monkeypatch.setattr(api, "mqtt_get", _get)

        assert await api.read_world_state("home/presence/kitchen") == {"present": False}
        assert asked == [("home/presence/kitchen", 2.0)]
