"""Turning up one logger's level from the dashboard, and what that must refuse.

Three rules, each because the obvious version is worse: never the root logger
(raising everything at DEBUG evicts the records you wanted from the buffer that
holds them), DEBUG only with the host's permission (it prints request bodies, so
a library at DEBUG writes credentials into a log this server serves), and
authenticated like every other write — which the middleware gives it for free by
living under `/api/`.
"""

import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import api_log_capture
from wactorz.web.app import build_app


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[TestClient, Any]:
    monkeypatch.delenv(api_log_capture.DEBUG_CAPTURE_ENV, raising=False)
    async with TestClient(TestServer(build_app())) as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_levels() -> Any:
    """Put back any level a test set — logging is process-wide state."""
    touched = ("aiomqtt", "litellm", "wactorz.test-target")
    before = {name: logging.getLogger(name).level for name in touched}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


class TestRaisingOneLogger:
    async def test_a_named_logger_is_set(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/logs/capture", json={"logger": "wactorz.test-target", "level": "WARNING"}
        )

        assert resp.status == 200
        assert logging.getLogger("wactorz.test-target").level == logging.WARNING

    async def test_raising_to_info_needs_no_flag(self, client: TestClient) -> None:
        # Only DEBUG is gated. Making a quiet library talk at INFO is the
        # ordinary case this endpoint exists for.
        resp = await client.post("/api/logs/capture", json={"logger": "aiomqtt", "level": "INFO"})

        assert resp.status == 200

    async def test_notset_restores_inheriting(self, client: TestClient) -> None:
        # The only way back without a restart.
        await client.post("/api/logs/capture", json={"logger": "aiomqtt", "level": "WARNING"})

        await client.post("/api/logs/capture", json={"logger": "aiomqtt", "level": "NOTSET"})

        assert logging.getLogger("aiomqtt").level == logging.NOTSET

    async def test_an_unknown_level_is_refused(self, client: TestClient) -> None:
        resp = await client.post("/api/logs/capture", json={"logger": "aiomqtt", "level": "LOUD"})

        assert resp.status == 400

    async def test_a_body_that_is_not_json_is_refused(self, client: TestClient) -> None:
        resp = await client.post("/api/logs/capture", data="logger=aiomqtt")

        assert resp.status == 400


class TestNeverGlobal:
    @pytest.mark.parametrize("name", ["", "root", "ROOT", ".", "   "])
    async def test_the_root_logger_is_refused(self, client: TestClient, name: str) -> None:
        resp = await client.post("/api/logs/capture", json={"logger": name, "level": "WARNING"})

        assert resp.status == 400

    async def test_the_refusal_says_why(self, client: TestClient) -> None:
        resp = await client.post("/api/logs/capture", json={"logger": "root", "level": "INFO"})

        assert "evicted" in (await resp.json())["error"]

    async def test_the_root_level_is_untouched(self, client: TestClient) -> None:
        before = logging.getLogger().level

        await client.post("/api/logs/capture", json={"logger": "", "level": "DEBUG"})

        assert logging.getLogger().level == before


class TestDebugNeedsTheHost:
    async def test_it_is_refused_without_the_flag(self, client: TestClient) -> None:
        resp = await client.post("/api/logs/capture", json={"logger": "litellm", "level": "DEBUG"})

        assert resp.status == 403
        assert logging.getLogger("litellm").level != logging.DEBUG

    async def test_the_refusal_names_the_flag_and_the_reason(self, client: TestClient) -> None:
        resp = await client.post("/api/logs/capture", json={"logger": "litellm", "level": "DEBUG"})

        body = (await resp.json())["error"]
        assert api_log_capture.DEBUG_CAPTURE_ENV in body
        assert "credentials" in body

    async def test_the_flag_allows_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set on the process, which is the point: it cannot be turned on
        # through the API someone is already talking to.
        monkeypatch.setenv(api_log_capture.DEBUG_CAPTURE_ENV, "1")

        resp = await client.post("/api/logs/capture", json={"logger": "litellm", "level": "DEBUG"})

        assert resp.status == 200
        assert logging.getLogger("litellm").level == logging.DEBUG


class TestReadingBackWhatIsSet:
    async def test_it_lists_a_logger_that_was_set(self, client: TestClient) -> None:
        await client.post(
            "/api/logs/capture", json={"logger": "wactorz.test-target", "level": "ERROR"}
        )

        payload = await (await client.get("/api/logs/capture")).json()

        assert payload["loggers"]["wactorz.test-target"] == "ERROR"

    async def test_it_reports_whether_debug_is_allowed(self, client: TestClient) -> None:
        payload = await (await client.get("/api/logs/capture")).json()

        assert payload["debug_allowed"] is False
