"""What a session cookie stands for, and for how long.

The cookie carries an id, never the configured key — so the interesting
properties are that an id is unguessable, that it stops working when revoked or
expired, and that revoking everything is one call rather than a credential
change that would break every script and probe at the same time.

Time is injected rather than slept, so the expiry tests are exact instead of
slow and flaky.
"""

from wactorz.web.sessions import (
    ONE_TIME_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    OneTimeCodes,
    SessionStore,
)


class TestStartingASession:
    def test_a_new_session_is_valid(self) -> None:
        store = SessionStore()

        assert store.is_valid(store.create())

    def test_two_sessions_get_different_ids(self) -> None:
        store = SessionStore()

        assert store.create() != store.create()

    def test_an_id_is_long_enough_that_guessing_is_not_a_threat(self) -> None:
        # 32 bytes hex-encoded. Stated as a test because it is the reason the
        # store needs no rate limiting of its own.
        assert len(SessionStore().create()) == 64

    def test_an_unknown_id_is_not_a_session(self) -> None:
        assert not SessionStore().is_valid("a" * 64)

    def test_an_empty_id_is_not_a_session(self) -> None:
        # What an absent cookie reads as: `request.cookies.get(...)` gives "".
        assert not SessionStore().is_valid("")


class TestExpiry:
    def test_a_session_expires_at_the_ttl(self) -> None:
        store = SessionStore(ttl_seconds=100)
        session = store.create(now=1_000.0)

        assert store.is_valid(session, now=1_099.0)
        assert not store.is_valid(session, now=1_100.0)

    def test_the_window_is_absolute_not_sliding(self) -> None:
        # A dashboard left open polls forever, so a window renewed on each
        # request would never close. Checking near the end must not extend it.
        store = SessionStore(ttl_seconds=100)
        session = store.create(now=0.0)

        assert store.is_valid(session, now=99.0)

        assert not store.is_valid(session, now=100.0)

    def test_an_expired_session_is_dropped_as_it_is_found(self) -> None:
        # Otherwise a client retrying a dead cookie grows the dict for as long
        # as it keeps trying.
        store = SessionStore(ttl_seconds=10)
        store.create(now=0.0)

        store.is_valid("nonexistent", now=100.0)
        assert len(store) == 1  # the retry did not clear anything on its own

        store.is_valid(next(iter(store._created)), now=100.0)
        assert len(store) == 0

    def test_the_default_window_is_a_month(self) -> None:
        # Long on purpose, and safe because it is revocable — one browser or
        # all of them. The in-memory store cuts it shorter on every restart.
        assert SESSION_TTL_SECONDS == 30 * 24 * 60 * 60


class TestEndingSessions:
    def test_revoking_one_leaves_the_others(self) -> None:
        store = SessionStore()
        first, second = store.create(), store.create()

        store.revoke(first)

        assert not store.is_valid(first)
        assert store.is_valid(second)

    def test_revoking_an_unknown_id_is_not_an_error(self) -> None:
        # Logging out twice, or with a cookie the server has already forgotten.
        SessionStore().revoke("never-existed")

    def test_revoke_all_ends_every_session_and_says_how_many(self) -> None:
        store = SessionStore()
        sessions = [store.create() for _ in range(3)]

        assert store.revoke_all() == 3

        assert not any(store.is_valid(s) for s in sessions)

    def test_revoke_all_on_an_empty_store_is_zero(self) -> None:
        assert SessionStore().revoke_all() == 0


class TestTheStartupSignInCode:
    def test_a_fresh_code_is_accepted(self) -> None:
        codes = OneTimeCodes()

        assert codes.redeem(codes.issue())

    def test_it_works_exactly_once(self) -> None:
        # The property that makes it safe to print: whoever reads the terminal
        # afterwards finds a code that has already been spent.
        codes = OneTimeCodes()
        code = codes.issue()

        assert codes.redeem(code)
        assert not codes.redeem(code)

    def test_it_expires(self) -> None:
        codes = OneTimeCodes(ttl_seconds=60)
        code = codes.issue(now=0.0)

        assert not codes.redeem(code, now=60.0)

    def test_an_expired_code_is_still_consumed(self) -> None:
        # Or a code seen twice could be the second use of a live one, depending
        # on which side of the window each attempt fell.
        codes = OneTimeCodes(ttl_seconds=60)
        code = codes.issue(now=0.0)

        codes.redeem(code, now=60.0)

        assert len(codes) == 0

    def test_an_unknown_code_is_refused(self) -> None:
        assert not OneTimeCodes().redeem("never-issued")

    def test_an_empty_code_is_refused(self) -> None:
        # What `?code=` with nothing after it reads as.
        assert not OneTimeCodes().redeem("")

    def test_two_codes_differ(self) -> None:
        codes = OneTimeCodes()

        assert codes.issue() != codes.issue()

    def test_the_window_is_minutes_not_days(self) -> None:
        # It lands in scrollback and in `docker logs`, so the window is the
        # thing keeping it from being a long-lived credential in plain text.
        assert ONE_TIME_TTL_SECONDS == 15 * 60
