"""What a session cookie stands for, and for how long.

The cookie carries an id, never the configured key — so the interesting
properties are that an id is unguessable, that it stops working when revoked or
expired, and that revoking everything is one call rather than a credential
change that would break every script and probe at the same time.

Time is injected rather than slept, so the expiry tests are exact instead of
slow and flaky.
"""

import hashlib
import stat
import time
from pathlib import Path

from wactorz.web.sessions import (
    ONE_TIME_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    SESSIONS_FILENAME,
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
        session = store.create(now=0.0)

        store.is_valid("nonexistent", now=100.0)
        assert len(store) == 1  # the retry did not clear anything on its own

        store.is_valid(session, now=100.0)
        assert len(store) == 0

    def test_the_default_window_is_a_month(self) -> None:
        # Long on purpose, and safe because it is revocable — one browser or
        # all of them.
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


class TestSurvivingARestart:
    """Sessions are kept on disk, so a bounce is not a sign-in ceremony.

    A "restart" here is a second `SessionStore` bound to the same directory,
    which is what the next process does.
    """

    def test_a_session_outlives_the_process_that_made_it(self, tmp_path: Path) -> None:
        first = SessionStore()
        first.bind(tmp_path, "the-configured-key")
        session_id = first.create()

        second = SessionStore()
        second.bind(tmp_path, "the-configured-key")

        assert second.is_valid(session_id)

    def test_the_file_never_holds_a_working_cookie(self, tmp_path: Path) -> None:
        store = SessionStore()
        store.bind(tmp_path, "the-configured-key")
        session_id = store.create()

        written = (tmp_path / SESSIONS_FILENAME).read_text()

        assert session_id not in written

    def test_only_this_user_can_read_it(self, tmp_path: Path) -> None:
        store = SessionStore()
        store.bind(tmp_path, "the-configured-key")
        store.create()

        mode = (tmp_path / SESSIONS_FILENAME).stat().st_mode

        assert stat.S_IMODE(mode) == 0o600

    def test_signing_out_here_signs_out_after_a_restart_too(self, tmp_path: Path) -> None:
        first = SessionStore()
        first.bind(tmp_path, "the-configured-key")
        session_id = first.create()
        first.revoke(session_id)

        second = SessionStore()
        second.bind(tmp_path, "the-configured-key")

        assert not second.is_valid(session_id)

    def test_signing_out_everywhere_outlives_the_restart(self, tmp_path: Path) -> None:
        first = SessionStore()
        first.bind(tmp_path, "the-configured-key")
        sessions = [first.create() for _ in range(3)]
        first.revoke_all()

        second = SessionStore()
        second.bind(tmp_path, "the-configured-key")

        assert not [s for s in sessions if second.is_valid(s)]

    def test_an_expired_session_is_not_restored(self, tmp_path: Path) -> None:
        first = SessionStore()
        first.bind(tmp_path, "the-configured-key")
        session_id = first.create(now=time.time() - SESSION_TTL_SECONDS - 1)

        second = SessionStore()
        second.bind(tmp_path, "the-configured-key")

        assert not second.is_valid(session_id)

    def test_expired_sessions_are_dropped_from_the_file(self, tmp_path: Path) -> None:
        # Otherwise a long-lived install accumulates every session it ever
        # issued, and the file only ever grows.
        first = SessionStore()
        first.bind(tmp_path, "the-configured-key")
        first.create(now=time.time() - SESSION_TTL_SECONDS - 1)

        second = SessionStore()
        second.bind(tmp_path, "the-configured-key")

        assert not len(second)


class TestRotatingTheKey:
    """Changing the API key ends every session, with nothing to remember to do.

    Rotating a credential is often *how* access gets revoked, so sessions that
    outlived it would quietly defeat the rotation.
    """

    def test_a_rotated_key_ends_existing_sessions(self, tmp_path: Path) -> None:
        first = SessionStore()
        first.bind(tmp_path, "the-original-key")
        session_id = first.create()

        second = SessionStore()
        second.bind(tmp_path, "the-rotated-key")

        assert not second.is_valid(session_id)

    def test_the_file_holds_no_fingerprint_of_the_key(self, tmp_path: Path) -> None:
        # Entries are keyed *under* the key rather than stored alongside a hash
        # of it: a stored fingerprint would be an offline guessing target for a
        # short key, which is exactly the key this warns about elsewhere.
        key = "the-configured-key"
        store = SessionStore()
        store.bind(tmp_path, key)
        store.create()

        written = (tmp_path / SESSIONS_FILENAME).read_text()

        assert hashlib.sha256(key.encode()).hexdigest() not in written
        assert key not in written


class TestWhenThereIsNoKeyToProtect:
    def test_an_install_with_no_key_writes_no_file(self, tmp_path: Path) -> None:
        # Every request is allowed through already, so the file would guard
        # nothing — and the default install gains nothing on disk.
        store = SessionStore()
        store.bind(tmp_path, "")
        store.create()

        assert not list(tmp_path.iterdir())

    def test_sessions_still_work_in_memory(self, tmp_path: Path) -> None:
        store = SessionStore()
        store.bind(tmp_path, "")

        assert store.is_valid(store.create())


class TestWhenTheFileIsUnusable:
    """A convenience cache must never be the reason the monitor will not start."""

    def test_a_corrupt_file_leaves_everyone_signed_out_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / SESSIONS_FILENAME).write_text("{not json at all")

        store = SessionStore()
        store.bind(tmp_path, "the-configured-key")

        assert not len(store)

    def test_a_file_of_the_wrong_shape_is_survived(self, tmp_path: Path) -> None:
        (tmp_path / SESSIONS_FILENAME).write_text('["a list, not a mapping"]')

        store = SessionStore()
        store.bind(tmp_path, "the-configured-key")

        assert not len(store)

    def test_sessions_still_work_after_an_unreadable_file(self, tmp_path: Path) -> None:
        (tmp_path / SESSIONS_FILENAME).write_text("{not json at all")
        store = SessionStore()
        store.bind(tmp_path, "the-configured-key")

        assert store.is_valid(store.create())

    def test_a_directory_that_cannot_be_written_does_not_break_signing_in(
        self, tmp_path: Path
    ) -> None:
        # The session still works for this process; it just will not outlive it.
        store = SessionStore()
        store.bind(tmp_path / "does-not-exist", "the-configured-key")

        assert store.is_valid(store.create())
