"""Every agent and node, sampled about once a minute into the metrics history.

The samples are what the dashboard already knows -- each agent's latest
heartbeat and metrics frame, each node's latest heartbeat -- written down, so a
trend survives a restart, and an install without Prometheus has one at all.
"""

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz import reset
from wactorz.core.persistence import WactorzDB
from wactorz.web import metrics_history, runtime
from wactorz.web.app import build_app
from wactorz.web.metrics_history import agent_samples, node_samples, record_once


def _entry(name: str, *, heard_ago: float = 0.0, **over: Any) -> dict[str, Any]:
    """An agent as the dashboard holds it after a heartbeat and a metrics frame."""
    return {
        "agent_id": f"{name}-id",
        "name": name,
        "state": "running",
        "mem": 42.5,
        "messages_processed": 7,
        "last_update": time.time() - heard_ago,
        "metrics": {
            "errors": 1,
            "tasks_completed": 5,
            "tasks_failed": 2,
            "queue_wait_p95_s": 0.25,
            "message_p95_s": 0.5,
        },
        **over,
    }


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(str(tmp_path / "wactorz.db")) as database:
        monkeypatch.setattr(runtime, "db", database)
        monkeypatch.setitem(runtime.state, "agents", {})
        monkeypatch.setattr(metrics_history, "known_nodes", list)
        yield database


class TestWhatIsSampled:
    def test_an_agent_heard_from_lately(self) -> None:
        (sample,) = agent_samples({"a": _entry("weather", node="rpi")}, time.time())

        assert sample["agent"] == "weather"
        assert sample["node"] == "rpi"
        assert sample["memory_mb"] == 42.5
        assert (sample["errors"], sample["tasks_completed"], sample["tasks_failed"]) == (1, 5, 2)
        assert sample["queue_wait_p95_s"] == 0.25
        assert sample["task_p95_s"] is None, "only a generated agent reports it"

    def test_not_one_gone_quiet(self) -> None:
        # Stopped, deleted, or on a node that went away: written down unchanged
        # for as long as it is listed, its trend would be a flat lie.
        quiet = _entry("old", heard_ago=metrics_history.HEARD_WITHIN_S + 1)

        assert agent_samples({"a": quiet}, time.time()) == []

    def test_a_value_that_is_not_a_number_is_left_empty(self) -> None:
        (sample,) = agent_samples({"a": _entry("odd", mem="lots", metrics="none")}, time.time())

        assert sample["memory_mb"] is None
        assert sample["errors"] is None

    def test_every_node_online_or_not(self) -> None:
        nodes = [
            {"node": "rpi", "online": True, "cpu_pct": 12.0, "agents": ["a", "b"]},
            {"node": "nuc", "online": False, "agents": []},
        ]

        samples = node_samples(nodes, time.time())

        assert [(s["node"], s["online"], s["agents"]) for s in samples] == [
            ("rpi", 1, 2),
            ("nuc", 0, 0),
        ]
        assert samples[0]["cpu_pct"] == 12.0

    def test_a_nodes_readings(self) -> None:
        nodes = [
            {
                "node": "rpi",
                "online": True,
                "swap_used_mb": 12,
                "load_1m": 0.5,
                "disk_free_mb": 3000,
                "temp_c": 61.2,
                "throttled": ["under_voltage", "throttled"],
            }
        ]

        (sample,) = node_samples(nodes, time.time())

        assert (sample["swap_used_mb"], sample["load_1m"], sample["disk_free_mb"]) == (
            12.0,
            0.5,
            3000.0,
        )
        assert sample["temp_c"] == 61.2
        assert sample["throttled"] == '["under_voltage", "throttled"]'

    @pytest.mark.parametrize(("flags", "stored"), [([], "[]"), (None, None)])
    def test_no_throttling_is_not_the_same_as_not_knowing(
        self, flags: list[str] | None, stored: str | None
    ) -> None:
        (sample,) = node_samples([{"node": "rpi", "throttled": flags}], time.time())

        assert sample["throttled"] == stored


class TestRecording:
    async def test_a_sample_is_written_and_read_back_oldest_first(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.state["agents"]["a"] = _entry("weather")
        monkeypatch.setattr(
            metrics_history, "known_nodes", lambda: [{"node": "rpi", "online": True}]
        )

        written = await record_once()
        runtime.state["agents"]["a"]["messages_processed"] = 9
        written += await record_once()

        history = db.query_agent_history("weather", time.time() - 60)
        assert written == 4
        assert [s["messages_processed"] for s in history] == [7, 9]
        assert len(db.query_node_history("rpi", time.time() - 60)) == 2

    async def test_nothing_known_writes_nothing(self, db: WactorzDB) -> None:
        assert await record_once() == 0

    async def test_without_a_database_nothing_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "db", None)

        assert await record_once() == 0


@pytest.fixture(name="client")
async def client_fixture(db: WactorzDB) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestReadingIt:
    async def test_an_agents_history(self, client: TestClient, db: WactorzDB) -> None:
        now = time.time()
        db.write_metrics_history(
            [
                {"ts": now - 7200, "agent": "weather", "messages_processed": 1},
                {"ts": now - 60, "agent": "weather", "messages_processed": 3},
            ],
            [],
        )

        body = await (await client.get("/api/history/agents/weather?hours=1")).json()

        assert body["agent"] == "weather"
        assert [s["messages_processed"] for s in body["samples"]] == [3]
        assert body["sample_every_s"] == metrics_history.SAMPLE_EVERY_S

    async def test_a_nodes_history(self, client: TestClient, db: WactorzDB) -> None:
        db.write_metrics_history([], [{"ts": time.time(), "node": "rpi", "online": 1}])

        body = await (await client.get("/history/nodes/rpi")).json()

        assert [s["online"] for s in body["samples"]] == [1]

    @pytest.mark.parametrize(
        ("stored", "served"),
        [
            ('["under_voltage"]', ["under_voltage"]),
            ("[]", []),
            (None, None),
            # Whatever a node sent is kept whole, a comma included.
            ('["a,b"]', ["a,b"]),
        ],
    )
    async def test_a_nodes_throttling_comes_back_as_it_went_in(
        self, client: TestClient, db: WactorzDB, stored: str | None, served: list[str] | None
    ) -> None:
        db.write_metrics_history(
            [], [{"ts": time.time(), "node": "rpi", "online": 1, "throttled": stored}]
        )

        body = await (await client.get("/history/nodes/rpi")).json()

        assert [s["throttled"] for s in body["samples"]] == [served]

    @pytest.mark.parametrize("hours", ["soon", "0", "-1", "inf", "nan"])
    async def test_a_window_that_is_not_one_is_refused(
        self, client: TestClient, hours: str
    ) -> None:
        response = await client.get(f"/api/history/agents/weather?hours={hours}")

        assert response.status == 400

    async def test_without_a_database_it_says_so(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "db", None)

        response = await client.get("/api/history/agents/weather")

        assert response.status == 503


class TestAMetricsReset:
    """The history records the counters a metrics reset zeroes; it goes with them."""

    def _seeded(self, path: Path) -> None:
        now = time.time()
        with WactorzDB(str(path)) as db:
            db.write_metrics_history(
                [{"ts": now, "agent": "weather"}, {"ts": now, "agent": "lights"}],
                [{"ts": now, "node": "rpi", "online": 1}],
            )

    def test_for_one_agent_clears_its_samples_only(self, tmp_path: Path) -> None:
        path = tmp_path / "w.db"
        self._seeded(path)

        reset.reset_metrics("weather", str(path))

        with WactorzDB(str(path)) as db:
            assert db.query_agent_history("weather", 0) == []
            assert db.query_agent_history("lights", 0) != []
            assert db.query_node_history("rpi", 0) != []

    def test_for_all_clears_every_sample(self, tmp_path: Path) -> None:
        path = tmp_path / "w.db"
        self._seeded(path)

        reset.reset_metrics(None, str(path))

        with WactorzDB(str(path)) as db:
            assert db.query_agent_history("lights", 0) == []
            assert db.query_node_history("rpi", 0) == []
