"""Browser sessions for the monitor: what the cookie stands for.

The cookie carries a random session id and **never the configured key**. The
key stays server-side, so a stolen cookie is one revocable session rather than
the credential itself, and "sign out everywhere" is a dict being cleared rather
than a secret being rotated.

Deliberately in memory only. A restart means everyone logs in again, which is
the honest trade for a store that cannot leak to disk, cannot drift out of sync
with a wiped state directory, and needs no migration. The installs this serves
restart often enough that persistence would buy little.

Nothing here reads configuration or touches aiohttp — it is a set of ids with
ages, so it can be tested for what it is.
"""

from __future__ import annotations

import secrets
import time

#: How long a session lasts, counted from when it was created rather than from
#: the last request. Absolute rather than sliding: a sliding window renews
#: itself for as long as anything keeps polling, and a dashboard left open in a
#: tab polls forever — so it would never expire at all.
#:
#: Thirty days is long for a credential and deliberate: it is revocable, both
#: for one browser and for all of them at once, and revocation is what makes a
#: long window safe. In practice restarts cut it far shorter — the store is in
#: memory — so this is a ceiling rather than a promise.
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

#: 32 bytes from `secrets`, hex-encoded. Long enough that guessing is not a
#: threat model, so the store needs no rate limit of its own.
_ID_BYTES = 32

#: How long the startup sign-in link stays usable. Short on purpose: it is
#: printed to a terminal, so it lives on in scrollback, in `docker logs`, and in
#: anything recording the screen. Miss the window and the login form with the
#: key is the way in — that fallback is why this can afford to be brief.
ONE_TIME_TTL_SECONDS = 15 * 60

#: URL-safe rather than hex: this one goes in a link a person may retype.
_CODE_BYTES = 32


class SessionStore:
    """Live session ids and when each began.

    One instance is created at import for the running server; tests build their
    own, so a test never has to reach into shared state to clear it.
    """

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._created: dict[str, float] = {}

    def create(self, now: float | None = None) -> str:
        """Start a session and return the id the cookie will carry."""
        session_id = secrets.token_hex(_ID_BYTES)
        self._created[session_id] = time.time() if now is None else now
        return session_id

    def is_valid(self, session_id: str, now: float | None = None) -> bool:
        """Whether this id is a live session.

        Expiry is enforced on read rather than by a sweeper: a session nobody
        presents does not matter, and one that is presented is checked here
        anyway. An expired entry is dropped as it is found, so the dict does not
        grow without bound for a caller that keeps retrying an old cookie.
        """
        if not session_id:
            return False
        started = self._created.get(session_id)
        if started is None:
            return False
        moment = time.time() if now is None else now
        if moment - started >= self._ttl:
            self._created.pop(session_id, None)
            return False
        return True

    def revoke(self, session_id: str) -> None:
        """End one session — logging out of this browser."""
        self._created.pop(session_id, None)

    def revoke_all(self) -> int:
        """End every session, returning how many there were.

        The "sign out all devices" primitive, and the reason the cookie holds an
        id rather than the key: signing everyone out is this, not a credential
        change that would also break every script and probe.
        """
        count = len(self._created)
        self._created.clear()
        return count

    def __len__(self) -> int:
        return len(self._created)


class OneTimeCodes:
    """Single-use sign-in codes, for the link printed at startup.

    A code is a credential with the shortest life the design allows: it works
    once, and only for a few minutes. That is what makes it safe to put in a
    terminal — the place it lands is scrollback, a container log a colleague can
    read, or a screen share.

    Redeeming burns the code whether or not it had expired, so a code that
    appears twice is never the second use of a live one.
    """

    def __init__(self, ttl_seconds: float = ONE_TIME_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._issued: dict[str, float] = {}

    def issue(self, now: float | None = None) -> str:
        """Mint a code for the sign-in link."""
        code = secrets.token_urlsafe(_CODE_BYTES)
        self._issued[code] = time.time() if now is None else now
        return code

    def redeem(self, code: str, now: float | None = None) -> bool:
        """Whether `code` was live, consuming it either way."""
        if not code:
            return False
        issued = self._issued.pop(code, None)
        if issued is None:
            return False
        moment = time.time() if now is None else now
        return moment - issued < self._ttl

    def __len__(self) -> int:
        return len(self._issued)


#: The running server's sessions.
store = SessionStore()

#: The running server's outstanding sign-in codes.
codes = OneTimeCodes()
