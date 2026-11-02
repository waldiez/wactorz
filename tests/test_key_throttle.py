"""A key in a header spends the same allowance per address as the sign-in form.

Over a real aiohttp application, because what is being held is the order of the
checks in the middleware: who is counted, who is refused before the comparison,
and who never reaches the count at all.
"""

import math
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config
from wactorz.web import auth, login, sessions, throttle

KEY = "the-right-key-for-these-tests"
ADDRESS = "127.0.0.1"


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _app() -> web.Application:
    app = web.Application(middlewares=[auth.auth_middleware])
    app.router.add_get("/api/actors", _ok)
    app.router.add_get("/login", login.login_page_handler)
    app.router.add_post("/login", login.login_submit_handler)
    return app


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[TestClient, Any]:
    monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, api_key=KEY))
    client = TestClient(TestServer(_app()))
    await client.start_server()
    yield client
    await client.close()


async def _status(client: TestClient, key: str) -> int:
    return (await client.get("/api/actors", headers={"X-API-Key": key})).status


def _lock_out() -> None:
    for _ in range(throttle.LOCKOUT_AFTER):
        throttle.throttle.record_failure(ADDRESS)


class TestWhatIsCounted:
    async def test_a_wrong_key_makes_the_next_attempt_wait(self, client: TestClient) -> None:
        assert await _status(client, "wrong") == 401
        assert throttle.throttle.retry_after(ADDRESS) > 0

    async def test_a_bearer_token_counts_as_the_header_does(self, client: TestClient) -> None:
        response = await client.get("/api/actors", headers={"Authorization": "Bearer wrong"})
        assert response.status == 401
        assert throttle.throttle.retry_after(ADDRESS) > 0

    async def test_a_key_that_is_not_ascii_is_a_wrong_key(self, client: TestClient) -> None:
        response = await client.get("/api/actors", headers={"X-API-Key": "κλειδί"})
        assert response.status == 401
        assert throttle.throttle.retry_after(ADDRESS) > 0

    @pytest.mark.parametrize("accept", ["application/json", "text/html"])
    async def test_a_request_with_no_key_is_not_an_attempt(
        self, client: TestClient, accept: str
    ) -> None:
        # A browser whose session ended, polling on: it must not lock itself
        # out of the form it is about to be sent to.
        for _ in range(throttle.LOCKOUT_AFTER + 1):
            response = await client.get(
                "/api/actors", headers={"Accept": accept}, allow_redirects=False
            )
            assert response.status in (302, 401)
        assert throttle.throttle.retry_after(ADDRESS) == 0.0

    async def test_the_right_key_forgets_earlier_failures(self, client: TestClient) -> None:
        throttle.throttle.record_failure(ADDRESS, now=0.0)
        assert await _status(client, KEY) == 200
        assert len(throttle.throttle) == 0

    async def test_old_failures_of_other_addresses_are_forgotten(self, client: TestClient) -> None:
        throttle.throttle.record_failure("192.0.2.1", now=0.0)
        await _status(client, "wrong")
        assert throttle.throttle.retry_after("192.0.2.1", now=0.0) == 0.0
        assert len(throttle.throttle) == 1


class TestARefusal:
    async def test_it_says_how_long_to_wait(self, client: TestClient) -> None:
        _lock_out()
        waiting = throttle.throttle.retry_after(ADDRESS)
        response = await client.get("/api/actors", headers={"X-API-Key": "wrong"})
        assert response.status == 429
        assert 0 < int(response.headers["Retry-After"]) <= math.ceil(waiting)

    async def test_the_right_key_waits_too(self, client: TestClient) -> None:
        # Otherwise a locked-out caller's next guess could still come back 200.
        _lock_out()
        assert await _status(client, KEY) == 429

    async def test_an_attempt_while_refused_is_not_counted(self, client: TestClient) -> None:
        _lock_out()
        waiting = throttle.throttle.retry_after(ADDRESS)
        assert await _status(client, "wrong") == 429
        assert throttle.throttle.retry_after(ADDRESS) <= waiting

    async def test_a_session_passes_a_lockout(self, client: TestClient) -> None:
        # A stale script on this machine must not sign the browser out with it.
        _lock_out()
        cookies = {auth.SESSION_COOKIE: sessions.store.create()}
        response = await client.get("/api/actors", headers={"X-API-Key": "wrong"}, cookies=cookies)
        assert response.status == 200

    async def test_failures_in_a_header_close_the_form(self, client: TestClient) -> None:
        _lock_out()
        response = await client.post("/login", data={"key": KEY}, allow_redirects=False)
        assert response.status == 429

    async def test_failures_on_the_form_close_the_header(self, client: TestClient) -> None:
        response = await client.post("/login", data={"key": "wrong"}, allow_redirects=False)
        assert response.status == 401
        assert await _status(client, KEY) == 429


class TestTheSignInPage:
    async def test_it_does_not_answer_for_a_key_in_a_header(self, client: TestClient) -> None:
        # The page needs no key, so a redirect here for the right one would
        # test guesses outside the count.
        response = await client.get("/login", headers={"X-API-Key": KEY}, allow_redirects=False)
        assert response.status == 200

    async def test_a_key_that_is_not_ascii_is_refused_on_the_form(self, client: TestClient) -> None:
        response = await client.post("/login", data={"key": "κλειδί"}, allow_redirects=False)
        assert response.status == 401
