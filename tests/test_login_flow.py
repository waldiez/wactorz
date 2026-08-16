"""Signing in: how a browser gets past the key check.

With `API_KEY` set, a request is answered 401 unless it carries the key or a
session. A browser can carry a cookie and not a header, so the cookie is its
way in — and it holds a session id, never the key, which keeps the credential
server-side and makes one browser revocable on its own.

Driven through the real app and a real client, because what matters here is
wiring: which routes are exempt, what an unauthenticated caller is answered
with, and whether the cookie a redirect carries is accepted on the next
request. None of that is visible from a handler called directly.

A test that finds the key in a `Set-Cookie` header is the one worth being
loudest about.
"""

from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from wactorz.config import CONFIG
from wactorz.web import auth, login, origins, sessions, throttle
from wactorz.web.app import build_app

KEY = "the-configured-key"


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[TestClient, Any]:
    """The real app with a key set — the configuration sign-in exists for."""
    monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))
    sessions.store.revoke_all()
    # A fresh counter per test: every case here signs in from the same address,
    # so a shared one would carry one test's failures into the next and answer
    # 429 where the test expected the key to be checked.
    monkeypatch.setattr(throttle, "throttle", throttle.LoginThrottle())
    async with TestClient(TestServer(build_app())) as c:
        yield c


def _cookie(client: TestClient) -> str:
    jar = {c.key: c.value for c in client.session.cookie_jar}
    return jar.get(auth.SESSION_COOKIE, "")


class TestWhatAnUnauthenticatedCallerIsTold:
    async def test_a_browser_is_sent_to_the_login_form(self, client: TestClient) -> None:
        resp = await client.get("/", headers={"Accept": "text/html"}, allow_redirects=False)

        assert resp.status == 302
        assert resp.headers["Location"].startswith("/login")

    async def test_a_script_still_gets_401(self, client: TestClient) -> None:
        # A redirect would hand curl an HTML page and a 200 it did not earn.
        resp = await client.get("/api/actors", headers={"Accept": "application/json"})

        assert resp.status == 401

    async def test_where_it_was_going_is_remembered(self, client: TestClient) -> None:
        # Asserted as a round trip rather than on the escaping: yarl normalises
        # the redirect URL, so how the value is encoded is its business — that
        # the value survives is ours.
        resp = await client.get(
            "/api/logs?level=ERROR", headers={"Accept": "text/html"}, allow_redirects=False
        )

        carried = parse_qs(urlparse(resp.headers["Location"]).query)["next"][0]
        assert carried == "/api/logs?level=ERROR"

    async def test_the_form_itself_is_reachable(self, client: TestClient) -> None:
        # Or the redirect loops: the page a stranger must reach cannot be gated.
        resp = await client.get("/login")

        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]

    async def test_health_stays_open(self, client: TestClient) -> None:
        assert (await client.get("/health")).status == 200


class TestSigningIn:
    async def test_the_right_key_starts_a_session(self, client: TestClient) -> None:
        resp = await client.post("/login", data={"key": KEY}, allow_redirects=False)

        assert resp.status == 302
        assert sessions.store.is_valid(_cookie(client))

    async def test_the_cookie_never_carries_the_key(self, client: TestClient) -> None:
        # The point of the whole design: a stolen cookie is one revocable
        # session, not the credential every script and probe also uses.
        resp = await client.post("/login", data={"key": KEY}, allow_redirects=False)

        assert KEY not in resp.headers.get("Set-Cookie", "")

    async def test_the_cookie_is_not_readable_from_script(self, client: TestClient) -> None:
        resp = await client.post("/login", data={"key": KEY}, allow_redirects=False)

        assert "HttpOnly" in resp.headers["Set-Cookie"]

    async def test_the_session_then_opens_the_dashboard(self, client: TestClient) -> None:
        await client.post("/login", data={"key": KEY})

        assert (await client.get("/api/actors")).status == 200

    async def test_a_wrong_key_is_refused_without_a_session(self, client: TestClient) -> None:
        resp = await client.post("/login", data={"key": "not-it"})

        assert resp.status == 401
        assert not _cookie(client)

    async def test_a_wrong_key_says_nothing_about_why(self, client: TestClient) -> None:
        resp = await client.post("/login", data={"key": "not-it"})

        body = await resp.text()
        assert "not accepted" in body
        assert KEY not in body

    async def test_it_returns_to_where_the_user_was_going(self, client: TestClient) -> None:
        resp = await client.post(
            "/login", data={"key": KEY, "next": "/api/logs"}, allow_redirects=False
        )

        assert resp.headers["Location"] == "/api/logs"

    async def test_it_will_not_be_redirected_off_this_server(self, client: TestClient) -> None:
        # The login page is the one page a stranger can always reach, so an
        # unchecked `next` would make it an open redirect.
        resp = await client.post(
            "/login", data={"key": KEY, "next": "https://elsewhere.example/"}, allow_redirects=False
        )

        assert resp.headers["Location"] == "/"

    async def test_a_scheme_relative_target_is_refused_too(self, client: TestClient) -> None:
        # `//host` starts with a slash, so a "must be absolute" check alone
        # waves it through while a browser reads it as another host.
        resp = await client.post(
            "/login", data={"key": KEY, "next": "//elsewhere.example/"}, allow_redirects=False
        )

        assert resp.headers["Location"] == "/"


class TestSigningOut:
    async def test_the_session_stops_working(self, client: TestClient) -> None:
        await client.post("/login", data={"key": KEY})
        assert (await client.get("/api/actors")).status == 200

        await client.post("/logout")

        assert (await client.get("/api/actors")).status == 401

    async def test_the_cookie_is_cleared_from_the_browser(self, client: TestClient) -> None:
        await client.post("/login", data={"key": KEY})

        await client.post("/logout")

        assert not _cookie(client)


class TestAnInstallWithNoKey:
    async def test_everything_stays_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default install is protected by the loopback bind, not a
        # credential — the sign-in work must not add a gate where there was none.
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=""))
        async with TestClient(TestServer(build_app())) as client:
            assert (await client.get("/api/actors")).status == 200

    async def test_the_login_page_does_not_ask_for_something_that_does_not_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=""))
        async with TestClient(TestServer(build_app())) as client:
            resp = await client.get("/login", allow_redirects=False)

            assert resp.status == 302


class TestTheStartupSignInLink:
    async def test_following_it_signs_the_browser_in(self, client: TestClient) -> None:
        code = sessions.codes.issue()

        resp = await client.get(f"/login?code={code}", allow_redirects=False)

        assert resp.status == 302
        assert sessions.store.is_valid(_cookie(client))

    async def test_following_it_twice_does_not(self, client: TestClient) -> None:
        # Whoever reads the terminal afterwards finds a spent code.
        code = sessions.codes.issue()
        await client.get(f"/login?code={code}", allow_redirects=False)
        client.session.cookie_jar.clear()

        resp = await client.get(f"/login?code={code}", allow_redirects=False)

        assert resp.status == 200
        assert not _cookie(client)

    async def test_a_spent_code_says_so(self, client: TestClient) -> None:
        # Rather than the generic prompt, which reads as "the link did nothing".
        resp = await client.get("/login?code=never-issued")

        assert "used or has expired" in await resp.text()

    async def test_it_honours_where_the_user_was_going(self, client: TestClient) -> None:
        code = sessions.codes.issue()

        resp = await client.get(f"/login?code={code}&next=/api/logs", allow_redirects=False)

        assert resp.headers["Location"] == "/api/logs"

    async def test_it_will_not_be_redirected_off_this_server(self, client: TestClient) -> None:
        code = sessions.codes.issue()

        resp = await client.get(
            f"/login?code={code}&next=https://elsewhere.example/", allow_redirects=False
        )

        assert resp.headers["Location"] == "/"


class TestTheLinkOnTheBanner:
    def test_it_is_offered_when_a_key_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))

        line = login.sign_in_line(8888)

        assert line is not None
        assert "/login?code=" in line

    def test_it_is_not_offered_when_there_is_nothing_to_sign_in_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=""))

        assert login.sign_in_line(8888) is None

    def test_the_code_it_offers_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))

        line = login.sign_in_line(8888)
        assert line is not None

        assert sessions.codes.redeem(line.split("code=")[1])

    def test_the_code_never_reaches_the_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        # The log file is unrotated and the dashboard serves its own log view, so
        # a sign-in link written there outlives the minutes it is meant to exist
        # for. The banner prints; it must not log.
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))

        with caplog.at_level("DEBUG"):
            line = login.sign_in_line(8888)

        assert line is not None
        assert "code=" not in caplog.text


class TestSigningOutEverywhere:
    async def test_every_session_ends(self, client: TestClient) -> None:
        await client.post("/login", data={"key": KEY})
        elsewhere = sessions.store.create()  # another browser

        await client.post("/logout/all")

        assert not sessions.store.is_valid(elsewhere)
        assert (await client.get("/api/actors")).status == 401

    async def test_the_key_still_works_afterwards(self, client: TestClient) -> None:
        # The point of holding a session id rather than the key: signing
        # everyone out must not break the scripts and probes using the key.
        await client.post("/login", data={"key": KEY})

        await client.post("/logout/all")

        resp = await client.get("/api/actors", headers={"X-API-Key": KEY})
        assert resp.status == 200


class TestGuessingGetsExpensive:
    """The backoff bites on the *second* attempt, not the fifth.

    A first wrong key costs a second, and that second is enough to refuse
    everything that follows it — so a burst of guesses never reaches the
    lockout count at all, because only the first of them is ever compared.
    Patience is what reaches the fifteen-minute lockout, and patience is what
    it is there to defeat.
    """

    async def test_a_second_guess_is_refused_without_being_checked(
        self, client: TestClient
    ) -> None:
        await client.post("/login", data={"key": "guess"})

        resp = await client.post("/login", data={"key": "guess"})

        assert resp.status == 429
        assert "Too many attempts" in await resp.text()

    async def test_the_wait_applies_to_the_right_key_too(self, client: TestClient) -> None:
        # Counting rather than delaying: the door is shut for the address, and
        # knowing the key does not reopen it early.
        await client.post("/login", data={"key": "guess"})

        resp = await client.post("/login", data={"key": KEY})

        assert resp.status == 429
        assert not _cookie(client)

    async def test_it_says_how_long_to_wait(self, client: TestClient) -> None:
        await client.post("/login", data={"key": "guess"})

        body = await (await client.post("/login", data={"key": "guess"})).text()

        assert "1 seconds" in body

    async def test_a_locked_out_address_is_told_in_minutes(self, client: TestClient) -> None:
        # Driven through the store, because reaching five failures over HTTP
        # means waiting out four backoffs first — which is the point of them.
        for _ in range(5):
            throttle.throttle.record_failure("127.0.0.1")

        body = await (await client.post("/login", data={"key": KEY})).text()

        assert "15 minutes" in body

    async def test_signing_in_clears_the_count(self, client: TestClient) -> None:
        # Seeded in the past so the wait has elapsed but the failure is still
        # counted: a typo followed later by the right key must leave nothing
        # behind, or the next typo costs more than a first one.
        throttle.throttle.record_failure("127.0.0.1", now=0.0)

        await client.post("/login", data={"key": KEY})

        assert throttle.throttle.retry_after("127.0.0.1") == 0.0


class TestWhatTheBrowserIsToldAboutSigningOut:
    """The dashboard cannot work out whether it holds a session by itself.

    "Is a key configured" is the wrong question: under Home Assistant ingress
    the user was authenticated by HA and carries no session here, so a sign-out
    would end nothing while implying it had.
    """

    async def test_a_signed_in_browser_is_offered_it(self, client: TestClient) -> None:
        await client.post("/login", data={"key": KEY})

        payload = await (await client.get("/api/config")).json()

        assert payload["auth"]["canSignOut"] is True

    async def test_an_install_with_no_key_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=""))
        async with TestClient(TestServer(build_app())) as client:
            payload = await (await client.get("/api/config")).json()

            assert payload["auth"]["canSignOut"] is False

    async def test_an_ingress_visitor_is_not(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Home Assistant authenticated them; there is no session here to end.
        monkeypatch.setattr(origins, "from_supervisor", lambda _request: True)

        payload = await (await client.get("/api/config")).json()

        assert payload["auth"]["canSignOut"] is False


class TestTheWebsocketHandshake:
    """The socket is behind the same gate as everything else.

    It carries the live feed and accepts chat and command frames, so leaving it
    open would make the key check decorative — a caller refused at `/api/actors`
    could simply open `/ws` and drive the same system.

    Nothing extra guards it: the middleware runs on the upgrade like any other
    request, which is the point of putting the check there rather than on each
    route. These pin that, because a handshake looks different enough from a
    request that "surely it needs its own check" is an easy conclusion to draw.
    """

    async def test_an_anonymous_socket_is_refused(self, client: TestClient) -> None:
        with pytest.raises(WSServerHandshakeError) as refused:
            async with client.ws_connect("/ws"):
                pass

        assert refused.value.status == 401

    async def test_a_signed_in_browser_may_open_it(self, client: TestClient) -> None:
        # The browser attaches the cookie to the upgrade itself; it cannot set
        # a header there, which is why the cookie is what makes this work.
        await client.post("/login", data={"key": KEY})

        async with client.ws_connect("/ws") as socket:
            assert not socket.closed

    async def test_a_script_may_open_it_with_the_key(self, client: TestClient) -> None:
        # No cookie and no browser: the header path has to work at the handshake
        # too, or every non-browser client is locked out of the live feed.
        async with client.ws_connect("/ws", headers={"X-API-Key": KEY}) as socket:
            assert not socket.closed

    async def test_a_revoked_session_cannot_reopen_it(self, client: TestClient) -> None:
        # What "sign out everywhere" has to mean for a socket: the open one is
        # unaffected, but it cannot be re-established.
        await client.post("/login", data={"key": KEY})
        sessions.store.revoke_all()

        with pytest.raises(WSServerHandshakeError) as refused:
            async with client.ws_connect("/ws"):
                pass

        assert refused.value.status == 401


class TestASessionSurvivesARestart:
    """The reason sessions are kept on disk, driven through the real app.

    A restart here is what the next process does: a new store over the same
    state directory, and a browser that still holds the cookie from before.
    """

    @staticmethod
    def _restart(monkeypatch: pytest.MonkeyPatch, state_dir: Path, key: str) -> None:
        """Stand up the store the next process would have."""
        store = sessions.SessionStore()
        store.bind(state_dir, key)
        monkeypatch.setattr(sessions, "store", store)

    async def test_a_cookie_from_the_previous_process_still_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))
        monkeypatch.setattr(throttle, "throttle", throttle.LoginThrottle())

        self._restart(monkeypatch, tmp_path, KEY)
        async with TestClient(TestServer(build_app())) as first:
            await first.post("/login", data={"key": KEY}, allow_redirects=False)
            cookie = _cookie(first)
        assert cookie

        self._restart(monkeypatch, tmp_path, KEY)
        async with TestClient(TestServer(build_app())) as second:
            resp = await second.get(
                "/api/actors",
                headers={"Accept": "application/json"},
                cookies={auth.SESSION_COOKIE: cookie},
            )

        assert resp.status == 200

    async def test_a_rotated_key_sends_that_browser_back_to_the_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))
        monkeypatch.setattr(throttle, "throttle", throttle.LoginThrottle())

        self._restart(monkeypatch, tmp_path, KEY)
        async with TestClient(TestServer(build_app())) as first:
            await first.post("/login", data={"key": KEY}, allow_redirects=False)
            cookie = _cookie(first)

        rotated = "the-key-after-rotation"
        monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=rotated))
        self._restart(monkeypatch, tmp_path, rotated)
        async with TestClient(TestServer(build_app())) as second:
            resp = await second.get(
                "/api/actors",
                headers={"Accept": "application/json"},
                cookies={auth.SESSION_COOKIE: cookie},
            )

        assert resp.status == 401
