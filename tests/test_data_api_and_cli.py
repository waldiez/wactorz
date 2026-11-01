"""What was recorded, over the API and from a terminal: the sources, a window's card, a copy.

``GET /api/data`` lists the sources with files, ``GET /api/data/{source}/profile``
is the dataset card of a window, and ``wactorz-data`` says the same from a
shell beside the server, or writes a window out as one file.
"""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core.paths import resolve_state_dir
from wactorz.data import data_root
from wactorz.data.cli import main
from wactorz.data.parquet import Writer
from wactorz.web.app import build_app

T0 = 1_760_000_000.0


@pytest.fixture(name="recorded")
def recorded_fixture() -> Path:
    """``sensors/imu/left`` with a minute of readings, under the state directory every test runs with."""
    root = data_root(resolve_state_dir())
    writer = Writer(root, "sensors/imu/left")
    for i in range(6):
        writer.append(
            {"ts": T0 + i * 10, "arrived": T0 + i * 10, "time_source": "payload", "ax": float(i)}
        )
    writer.flush()
    return root


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


class TestTheRoutes:
    async def test_the_sources_are_listed(self, client: TestClient, recorded: Path) -> None:
        body = await (await client.get("/api/data")).json()

        (found,) = body["sources"]
        assert (found["source"], found["hours"], found["files"]) == ("sensors/imu/left", 1, 1)
        assert found["bytes"] > 0
        assert (found["from"], found["to"]) == (T0 - T0 % 3600, T0 - T0 % 3600 + 3600)
        assert body["retention_days"] is None

    async def test_a_window_is_profiled(self, client: TestClient, recorded: Path) -> None:
        body = await (
            await client.get(
                "/api/data/sensors/imu/left/profile", params={"since": T0, "until": T0 + 31}
            )
        ).json()

        assert (body["source"], body["rows"], body["since"], body["until"]) == (
            "sensors/imu/left",
            4,
            T0,
            T0 + 31,
        )
        assert body["columns"]["ax"]["max"] == 3.0

    async def test_the_last_day_is_the_window_when_unsaid(
        self, client: TestClient, recorded: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import wactorz.web.api_data as module

        monkeypatch.setattr(module.time, "time", lambda: T0 + 3600)

        body = await (await client.get("/api/data/sensors/imu/left/profile")).json()

        assert body["rows"] == 6
        assert body["until"] == T0 + 3600

    @pytest.mark.parametrize(
        ("path", "params", "words"),
        [
            ("/api/data/sensors/imu/left/profile", {"since": "soon"}, "Unix seconds"),
            ("/api/data/sensors/imu/left/profile", {"since": T0, "until": T0 - 1}, "comes after"),
            ("/api/data/-x/profile", {}, "cannot name a source"),
        ],
    )
    async def test_a_bad_ask_is_refused(
        self, client: TestClient, recorded: Path, path: str, params: dict, words: str
    ) -> None:
        response = await client.get(path, params=params)

        assert response.status == 400
        assert words in (await response.json())["error"]

    async def test_a_source_with_nothing_is_an_empty_card(
        self, client: TestClient, recorded: Path
    ) -> None:
        body = await (await client.get("/api/data/sensors/none/profile")).json()

        assert body["rows"] == 0


@pytest.fixture(name="caplog")
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The command's output is its log at INFO, which is what is read back."""
    caplog.set_level(logging.INFO, logger="wactorz.data.cli")
    return caplog


class TestTheCommandLine:
    def test_sources_and_the_card_are_printed(
        self, recorded: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state = str(Path(resolve_state_dir()))

        assert main(["sources", "--state-dir", state]) == 0
        assert (
            main(
                [
                    "profile",
                    "sensors/imu/left",
                    "--state-dir",
                    state,
                    "--since",
                    str(T0),
                    "--until",
                    str(T0 + 100),
                ]
            )
            == 0
        )

        assert "sensors/imu/left" in caplog.text
        assert "6 rows" in caplog.text
        assert "ax: number, 6 values" in caplog.text

    def test_the_card_as_json(self, recorded: Path, caplog: pytest.LogCaptureFixture) -> None:
        state = str(Path(resolve_state_dir()))

        assert (
            main(
                [
                    "profile",
                    "sensors/imu/left",
                    "--state-dir",
                    state,
                    "--since",
                    str(T0),
                    "--until",
                    str(T0 + 100),
                    "--json",
                ]
            )
            == 0
        )

        card = json.loads(caplog.records[-1].getMessage())
        assert (card["rows"], card["since"]) == (6, T0)

    def test_a_window_is_exported_as_one_file(self, recorded: Path, tmp_path: Path) -> None:
        state = str(Path(resolve_state_dir()))
        out = tmp_path / "out" / "imu.parquet"

        assert (
            main(
                [
                    "export",
                    "sensors/imu/left",
                    str(out),
                    "--state-dir",
                    state,
                    "--since",
                    str(T0),
                    "--until",
                    str(T0 + 25),
                ]
            )
            == 0
        )

        assert pq.read_table(out).column("ax").to_pylist() == [0.0, 1.0, 2.0]

    def test_a_bad_source_fails_and_says_why(self, caplog: pytest.LogCaptureFixture) -> None:
        assert main(["profile", "../x", "--state-dir", resolve_state_dir()]) == 1
        assert "cannot name a source" in caplog.text
