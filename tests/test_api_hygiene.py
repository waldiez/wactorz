"""What a failing endpoint tells the caller, and what it keeps to itself.

An exception string answers a different question from the one the caller asked.
A sqlite error carries the database file's path; a third-party client's error
can carry a URL. Both were being handed back in the response body, where the
caller is the least entitled reader of them.

The detail is not lost — it is logged, where the operator can see it and a
stranger cannot.
"""

from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import runtime
from wactorz.web.app import build_app


@pytest.fixture(name="client")
async def client_fixture() -> Any:
    async with TestClient(TestServer(build_app())) as c:
        yield c


class TestAFailureKeepsItsDetailsToItself:
    async def test_a_broken_chat_log_query_does_not_return_the_exception(
        self, client: TestClient, caplog: Any
    ) -> None:
        leaky = RuntimeError("unable to open database file: /data/state/wactorz.db")

        with patch.object(runtime, "db") as db:
            db.query_chat_log.side_effect = leaky
            with caplog.at_level("ERROR"):
                resp = await client.get("/api/chats")

        assert resp.status == 500
        body = await resp.text()
        assert "/data/state" not in body
        # Kept, not lost — the operator needs it to fix the thing.
        assert "/data/state" in caplog.text

    async def test_a_bad_cost_limit_is_told_what_to_send(self, client: TestClient) -> None:
        # A 400 still has to be actionable, or the caller cannot correct it.
        resp = await client.post("/api/cost/limit", json={"limit_usd": "not-a-number"})

        assert resp.status == 400
        assert "limit_usd" in (await resp.json())["error"]

    async def test_a_bad_cost_limit_does_not_quote_python(self, client: TestClient) -> None:
        resp = await client.post("/api/cost/limit", json={"limit_usd": "not-a-number"})

        assert "could not convert" not in (await resp.json())["error"]
