"""Serving the application log.

The endpoint is a read over the in-memory ring buffer, and every dimension a
caller controls is bounded here rather than trusted: `limit` is clamped, the
level filter accepts only names we know, and the logger filter is a substring
match over a field the server wrote.

⚠ These records are not chat. A log carries what nobody chose to put there —
resolved config, paths, third-party output — so what this route serves, and to
whom, is a different question from the rest of the API. The redaction that
guards it happens on the way into the buffer, not here.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.monitoring import log_buffer
from wactorz.web import api_logs


@pytest.fixture(name="buffer")
def buffer_fixture() -> Iterator[log_buffer.LogRingBuffer]:
    """A freshly installed buffer, detached again afterwards."""
    log_buffer.uninstall()
    installed = log_buffer.install(capacity=50)
    yield installed
    log_buffer.uninstall()


@pytest.fixture(name="client")
async def client_fixture(buffer: log_buffer.LogRingBuffer) -> AsyncIterator[TestClient[Any, Any]]:
    app = web.Application()
    app.router.add_get("/api/logs", api_logs.logs_handler)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


def _record(level: int, name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, "x.py", 1, message, None, None)


def _fill(buffer: log_buffer.LogRingBuffer, *specs: tuple[int, str, str]) -> None:
    for level, name, message in specs:
        buffer.emit(_record(level, name, message))


async def _get(client: TestClient[Any, Any], query: str = "") -> dict[str, Any]:
    resp = await client.get(f"/api/logs{query}")
    assert resp.status == 200
    return await resp.json()


class TestWhatItServes:
    async def test_the_buffered_records_oldest_first(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        _fill(
            buffer, (logging.INFO, "wactorz.web", "first"), (logging.INFO, "wactorz.web", "second")
        )

        body = await _get(client)

        assert [e["text"] for e in body["entries"]] == ["first", "second"]

    async def test_the_envelope_the_feed_renders(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        _fill(buffer, (logging.WARNING, "wactorz.agents.installer", "careful"))

        (entry,) = (await _get(client))["entries"]

        assert entry["source"] == "app"
        assert entry["level"] == "WARNING"
        assert entry["origin"] == "wactorz.agents.installer"
        assert entry["text"] == "careful"
        assert isinstance(entry["ts"], float)

    async def test_an_empty_buffer_is_not_an_error(self, client: TestClient[Any, Any]) -> None:
        body = await _get(client)

        assert not body["entries"]

    async def test_no_buffer_installed_is_not_an_error(self, client: TestClient[Any, Any]) -> None:
        # A process that never called install() has nothing recorded — which is
        # a state, not a failure.
        log_buffer.uninstall()

        body = await _get(client)

        assert not body["entries"]
        assert body["capacity"] == 0


class TestTheCallerDoesNotChooseTheResponseSize:
    @pytest.mark.parametrize(
        ("asked", "expected"),
        [
            ("?limit=5", 5),
            (f"?limit={api_logs.MAX_LIMIT + 1000}", api_logs.MAX_LIMIT),
            ("?limit=0", 1),
            ("?limit=-20", 1),
            ("?limit=all", 10),
            ("", 10),
        ],
    )
    async def test_limit_is_clamped(
        self,
        client: TestClient[Any, Any],
        buffer: log_buffer.LogRingBuffer,
        asked: str,
        expected: int,
    ) -> None:
        # ⚠ The cap is the point: a request must not be able to name how much
        # work the server does building a response.
        _fill(buffer, *[(logging.INFO, "wactorz", f"line {i}") for i in range(10)])

        body = await _get(client, asked)

        assert len(body["entries"]) == min(expected, 10)

    async def test_it_returns_the_most_recent(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        _fill(buffer, *[(logging.INFO, "wactorz", f"line {i}") for i in range(10)])

        body = await _get(client, "?limit=3")

        assert [e["text"] for e in body["entries"]] == ["line 7", "line 8", "line 9"]


class TestFiltering:
    async def test_level_is_this_and_above(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        _fill(
            buffer,
            (logging.DEBUG, "wactorz", "chatty"),
            (logging.WARNING, "wactorz", "careful"),
            (logging.ERROR, "wactorz", "broken"),
        )

        body = await _get(client, "?level=WARNING")

        assert [e["text"] for e in body["entries"]] == ["careful", "broken"]

    async def test_an_unknown_level_shows_everything(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        # Emptying the view over an unrecognised filter is the wrong answer for
        # someone who opened it to diagnose something.
        _fill(buffer, (logging.INFO, "wactorz", "hello"))

        body = await _get(client, "?level=BANANA")

        assert len(body["entries"]) == 1

    async def test_logger_matches_a_substring_case_insensitively(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        _fill(
            buffer,
            (logging.INFO, "wactorz.agents.installer", "mine"),
            (logging.INFO, "aiohttp.access", "theirs"),
        )

        body = await _get(client, "?logger=AGENTS")

        assert [e["text"] for e in body["entries"]] == ["mine"]

    async def test_filters_apply_before_the_limit(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        # `limit=2&level=ERROR` means the two most recent errors, not the errors
        # among the two most recent records — otherwise a burst of INFO hides
        # every error from the view.
        _fill(buffer, (logging.ERROR, "wactorz", "boom"))
        _fill(buffer, *[(logging.INFO, "wactorz", f"noise {i}") for i in range(20)])

        body = await _get(client, "?limit=2&level=ERROR")

        assert [e["text"] for e in body["entries"]] == ["boom"]


class TestRedactionReachesTheResponse:
    async def test_a_credential_does_not_come_back(
        self, client: TestClient[Any, Any], buffer: log_buffer.LogRingBuffer
    ) -> None:
        # Redaction happens on the way into the buffer; this pins that the
        # endpoint serves the redacted copy rather than re-reading a raw record.
        _fill(buffer, (logging.INFO, "wactorz", "connecting with password=hunter2"))

        body = await _get(client, "")

        assert "hunter2" not in str(body)
