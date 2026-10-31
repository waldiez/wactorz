"""Old hours of readings go after a retention, except the ones a stored model learned from.

A retention is off unless set. With one, the sweep removes the hours older than
it, source by source, keeps every hour inside a window a version's metadata
names as its training data, and leaves no empty directories behind. Main runs
it on a cadence and says what went; `wactorz-data sweep` does it by hand. What
each source holds, and a column over time in buckets, are what the data tab
reads. ``docs/design/data-storage.md`` ("Old hours go, except a model's") is
the decision these pin.
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.agents.main import data_retention as main_side
from wactorz.agents.main.data_retention import DataRetention
from wactorz.agents.main.hosts import ArtifactHost
from wactorz.core.actor import ActorState
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.paths import resolve_state_dir
from wactorz.data import data_root
from wactorz.data.cli import main
from wactorz.data.files import HOUR_S, hours_of, summaries, summary
from wactorz.data.frames import SplitError, series
from wactorz.data.parquet import Writer
from wactorz.data.retention import is_protected, protected_windows, sweep
from wactorz.web.app import build_app

#: 2025-10-09 08:00:00 UTC, on the hour.
T0 = 1_759_996_800.0
DAY = 86400.0


def _record(root: Path, source: str, hours: list[float]) -> None:
    """One file per hour in ``hours`` for ``source``."""
    writer = Writer(root, source)
    for hour in hours:
        writer.append({"ts": hour + 1, "arrived": hour + 1, "time_source": "payload", "x": 1.0})
        writer.flush()


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    return data_root(tmp_path / "state")


class TestWhatASourceHolds:
    def test_the_hours_and_what_each_holds_are_listed(self, root: Path) -> None:
        _record(root, "sensors/imu", [T0, T0 + HOUR_S, T0 + 3 * HOUR_S])
        writer = Writer(root, "sensors/imu")
        writer.append({"ts": T0 + 2, "arrived": T0, "time_source": "payload", "x": 2.0})
        writer.flush()  # a further part of the first hour

        found = hours_of(root, "sensors/imu")

        assert [(hour, len(paths)) for hour, paths in found] == [
            (T0, 2),
            (T0 + HOUR_S, 1),
            (T0 + 3 * HOUR_S, 1),
        ]
        card = summary(root, "sensors/imu")
        assert (card["hours"], card["files"], card["from"], card["to"]) == (
            3,
            4,
            T0,
            T0 + 4 * HOUR_S,
        )
        assert card["bytes"] > 0
        assert [c["source"] for c in summaries(root)] == ["sensors/imu"]
        assert summary(root, "sensors/none") == {
            "source": "sensors/none",
            "hours": 0,
            "files": 0,
            "bytes": 0,
            "from": None,
            "to": None,
        }


class TestTheSweep:
    def test_hours_older_than_the_retention_go_and_the_rest_stay(self, root: Path) -> None:
        _record(root, "a", [T0, T0 + DAY, T0 + 10 * DAY])
        _record(root, "b", [T0 + 2 * DAY])

        report = sweep(root, keep_days=5, now=T0 + 10 * DAY + HOUR_S)

        assert report.removed == [("a", T0), ("a", T0 + DAY), ("b", T0 + 2 * DAY)]
        assert report.bytes > 0
        assert [h for h, _ in hours_of(root, "a")] == [T0 + 10 * DAY]
        assert hours_of(root, "b") == []
        # No empty directories are left behind a source that lost everything.
        assert not (root / "b").exists()
        assert (root / "a").is_dir()

    def test_a_dry_run_removes_nothing_and_says_what_would_go(self, root: Path) -> None:
        _record(root, "a", [T0])

        report = sweep(root, keep_days=1, now=T0 + 3 * DAY, dry_run=True)

        assert (report.removed, report.dry_run) == ([("a", T0)], True)
        assert [h for h, _ in hours_of(root, "a")] == [T0]

    def test_without_a_retention_nothing_goes(self, root: Path) -> None:
        _record(root, "a", [T0])

        assert sweep(root, keep_days=0, now=T0 + 100 * DAY).removed == []
        assert [h for h, _ in hours_of(root, "a")] == [T0]

    def test_an_hour_a_model_was_trained_on_stays(self, root: Path) -> None:
        _record(root, "a", [T0, T0 + HOUR_S, T0 + 2 * HOUR_S])
        windows = [("a", T0 + HOUR_S + 600, T0 + 2 * HOUR_S + 1)]

        report = sweep(root, keep_days=1, protected=windows, now=T0 + 3 * DAY)

        assert report.removed == [("a", T0)]
        assert report.protected == 2
        assert [h for h, _ in hours_of(root, "a")] == [T0 + HOUR_S, T0 + 2 * HOUR_S]
        assert is_protected("a", T0 + HOUR_S, windows)
        assert not is_protected("b", T0 + HOUR_S, windows)

    def test_the_windows_come_from_the_versions_metadata(self, tmp_path: Path) -> None:
        store = ArtifactStore.in_state_dir(tmp_path / "state")
        weights = tmp_path / "w.bin"
        weights.write_bytes(b"w")
        store.add(
            weights,
            "imu",
            format_name="raw",
            metadata={"data": {"source": "a", "since": 1.0, "until": 2.0}},
        )
        store.add(
            weights,
            "imu",
            format_name="raw",
            metadata={"data": [{"source": "a", "since": 3, "until": 4}, {"source": 5}, "no"]},
        )
        store.add(weights, "other", format_name="raw")

        assert protected_windows(store) == [("a", 1.0, 2.0), ("a", 3.0, 4.0)]


class _Host:
    def __init__(self) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self.published: list[tuple[str, Any]] = []

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    def feed(self) -> list[str]:
        return [p["message"] for t, p in self.published if t == "agents/main-id/logs"]


class TestMainSweeps:
    async def test_a_sweep_says_what_went_and_keeps_a_models_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = Path(resolve_state_dir())
        root = data_root(state)
        _record(root, "a", [T0, T0 + HOUR_S])
        store = ArtifactStore.in_state_dir(state)
        weights = state / "w.bin"
        state.mkdir(parents=True, exist_ok=True)
        weights.write_bytes(b"w")
        store.add(
            weights,
            "imu",
            format_name="raw",
            metadata={"data": {"source": "a", "since": T0 + HOUR_S, "until": T0 + 2 * HOUR_S}},
        )
        monkeypatch.setattr(main_side.time, "time", lambda: T0 + 30 * DAY)
        import wactorz.data.retention as retention_module

        monkeypatch.setattr(retention_module.time, "time", lambda: T0 + 30 * DAY)
        host = _Host()
        # The fake declares what the sweep reads; it stands in for MainActor.
        keeper = DataRetention(cast("ArtifactHost", host), keep_days=7)

        report = await keeper.once()

        assert report.removed == [("a", T0)]
        assert host.feed() == [
            "let go of 1 hour(s) of readings older than 7 day(s), "
            f"{report.bytes} bytes; kept 1 a model was trained on"
        ]
        assert keeper.last is report

    async def test_without_a_retention_the_loop_returns_at_once(self) -> None:
        keeper = DataRetention(cast("ArtifactHost", _Host()), keep_days=0)

        await keeper.run()  # returns rather than sleeping

        assert keeper.last is None


class TestASeries:
    def test_a_column_is_bucketed_over_the_window(self) -> None:
        frame = pa.table(
            {"ts": pa.array([T0, T0 + 1, T0 + 2, T0 + 8], pa.float64()), "x": [1.0, 3.0, None, 5.0]}
        )

        drawn = series(frame, "x", since=T0, until=T0 + 10, points=5)

        buckets = drawn["buckets"]
        assert [b["count"] for b in buckets] == [2, 1, 0, 0, 1]
        assert (buckets[0]["mean"], buckets[0]["min"], buckets[0]["max"]) == (2.0, 1.0, 3.0)
        assert buckets[1] == {
            "from": T0 + 2,
            "to": T0 + 4,
            "count": 1,
            "mean": None,
            "min": None,
            "max": None,
        }
        assert buckets[2]["count"] == 0 and buckets[2]["mean"] is None
        with pytest.raises(SplitError, match="no column 'y'"):
            series(frame, "y", since=T0, until=T0 + 10)


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheRoutes:
    async def test_the_series_route_draws_a_column(self, client: TestClient) -> None:
        root = data_root(resolve_state_dir())
        writer = Writer(root, "sensors/imu")
        for i in range(4):
            writer.append({"ts": T0 + i, "arrived": T0, "time_source": "payload", "x": float(i)})
        writer.flush()

        body = await (
            await client.get(
                "/api/data/sensors/imu/series",
                params={"column": "x", "since": T0, "until": T0 + 4, "points": 2},
            )
        ).json()

        assert body["source"] == "sensors/imu"
        assert [b["mean"] for b in body["buckets"]] == [0.5, 2.5]
        missing = await client.get(
            "/api/data/sensors/imu/series", params={"column": "y", "since": T0, "until": T0 + 4}
        )
        assert missing.status == 404
        assert (await client.get("/api/data/sensors/imu/series")).status == 400
        bad = await client.get(
            "/api/data/sensors/imu/series", params={"column": "x", "points": "many"}
        )
        assert bad.status == 400

    async def test_the_retention_in_force_is_said(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import wactorz.web.api_data as module

        monkeypatch.setattr(module, "DATA_RETENTION_DAYS", 30.0)

        body = await (await client.get("/api/data")).json()

        assert body == {"sources": [], "retention_days": 30.0}


class TestTheCommandLine:
    def test_sweep_says_what_would_go_then_removes_it(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.data.cli")
        state = Path(resolve_state_dir())
        root = data_root(state)
        _record(root, "a", [T0])
        import wactorz.data.retention as retention_module

        monkeypatch.setattr(retention_module.time, "time", lambda: T0 + 30 * DAY)

        assert main(["sweep", "--days", "7", "--dry-run", "--state-dir", str(state)]) == 0
        assert "would remove 1 hour(s)" in caplog.text
        assert [h for h, _ in hours_of(root, "a")] == [T0]
        assert main(["sweep", "--days", "7", "--state-dir", str(state)]) == 0
        assert hours_of(root, "a") == []
        assert main(["sweep", "--state-dir", str(state)]) == 1
        assert "no retention" in caplog.text

    def test_sources_say_what_they_hold(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="wactorz.data.cli")
        state = Path(resolve_state_dir())
        _record(data_root(state), "a", [T0])

        assert main(["sources", "--state-dir", str(state)]) == 0

        assert "a: 1 hour(s)" in caplog.text
        assert "2025-10-09 08:00:00 UTC to 2025-10-09 09:00:00 UTC" in caplog.text
