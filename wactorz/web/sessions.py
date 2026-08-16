"""Browser sessions for the monitor: what the cookie stands for.

The cookie carries a random session id and **never the configured key**. The
key stays server-side, so a stolen cookie is one revocable session rather than
the credential itself, and "sign out everywhere" is a dict being cleared rather
than a secret being rotated.

Sessions outlive a restart, because the alternative is a sign-in ceremony every
time the process bounces — and a credential a person retypes that often is one
they end up storing somewhere worse. `bind` is what turns persistence on; until
it is called this is a plain dict, which is how the tests take it.

What lands on disk is deliberately not the session ids. Entries are keyed by
``HMAC-SHA256(api_key, session_id)``, which does two jobs at once: the file
cannot be read for working cookies, and rotating the key stops every stored
entry from matching anything a browser presents — so rotating a credential
still means what an operator expects it to mean, with nothing here to remember
to do about it.

Nothing here reads configuration or touches aiohttp — it is a set of ids with
ages, so it can be tested for what it is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: How long a session lasts, counted from when it was created rather than from
#: the last request. Absolute rather than sliding: a sliding window renews
#: itself for as long as anything keeps polling, and a dashboard left open in a
#: tab polls forever — so it would never expire at all.
#:
#: Thirty days is long for a credential and deliberate: it is revocable, both
#: for one browser and for all of them at once, and revocation is what makes a
#: long window safe. Now that the store survives a restart this is the real
#: window rather than a ceiling restarts kept cutting short.
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

#: Where sessions are kept, inside the state directory. A plain name at the top
#: of it: a wipe clears chat, metrics, spawns, pickles and logs by naming each,
#: so nothing there matches this — and being signed out by "wipe all" would read
#: as a bug rather than as the reset it belongs to.
SESSIONS_FILENAME = "sessions.json"

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
        #: Keyed by `_token`, never by the id itself, so what is held in memory
        #: is what gets written and neither is a working cookie.
        self._tokens: dict[str, float] = {}
        self._path: Path | None = None
        self._secret = b""

    def bind(self, state_dir: str | Path, api_key: str) -> None:
        """Keep sessions in `state_dir`, and load the ones already there.

        Called once at startup, before anything is served: it replaces whatever
        is held, and it fixes the key entries are derived under.

        With no key configured this does nothing and the store stays in memory.
        That install allows every request through already, so a sessions file
        would protect nothing and the default install gains no new file on disk.
        """
        if not api_key:
            return
        self._secret = api_key.encode()
        self._path = Path(state_dir) / SESSIONS_FILENAME
        self._load()

    def _token(self, session_id: str) -> str:
        """What is stored for `session_id`.

        Keyed rather than plainly hashed. A bare digest of a 32-byte id would
        already be safe from guessing, but keying it means the entries also stop
        matching when the key changes — so a rotated key ends every session
        without a stored fingerprint of the key to be attacked offline.
        """
        return hmac.new(self._secret, session_id.encode(), hashlib.sha256).hexdigest()

    def _load(self) -> None:
        """Read stored sessions, dropping any that have expired.

        A file that cannot be read leaves the store empty rather than raising:
        the cost is that everyone signs in again, and refusing to start the
        monitor because a convenience cache is malformed is far worse than that.
        """
        if self._path is None or not self._path.is_file():
            return
        try:
            stored = {str(k): float(v) for k, v in json.loads(self._path.read_text()).items()}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("[auth] could not read stored sessions (%s); starting empty", exc)
            return
        now = time.time()
        self._tokens = {token: at for token, at in stored.items() if now - at < self._ttl}

    def _save(self) -> None:
        """Write the store out, atomically and readable only by this user.

        Opened at 0600 rather than chmod-ed afterwards: creating it at the
        umask's permissions and narrowing them after leaves a window where the
        file is readable, which is the whole thing this is trying to avoid.
        """
        if self._path is None:
            return
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self._tokens, handle)
            # Replaced rather than written in place, so a crash mid-write leaves
            # the previous file whole instead of a truncated one nobody can read.
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("[auth] could not persist sessions: %s", exc)
            tmp.unlink(missing_ok=True)

    def create(self, now: float | None = None) -> str:
        """Start a session and return the id the cookie will carry."""
        session_id = secrets.token_hex(_ID_BYTES)
        self._tokens[self._token(session_id)] = time.time() if now is None else now
        self._save()
        return session_id

    def is_valid(self, session_id: str, now: float | None = None) -> bool:
        """Whether this id is a live session.

        Expiry is enforced on read rather than by a sweeper: a session nobody
        presents does not matter, and one that is presented is checked here
        anyway. An expired entry is dropped as it is found, so the dict does not
        grow without bound for a caller that keeps retrying an old cookie.

        Dropping one does not write the file. This runs on every request, and an
        entry that has expired is refused whether or not it is still on disk —
        the next session started or ended writes it out, and `_load` drops it
        again anyway.
        """
        if not session_id:
            return False
        token = self._token(session_id)
        started = self._tokens.get(token)
        if started is None:
            return False
        moment = time.time() if now is None else now
        if moment - started >= self._ttl:
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, session_id: str) -> None:
        """End one session — logging out of this browser."""
        if self._tokens.pop(self._token(session_id), None) is not None:
            self._save()

    def revoke_all(self) -> int:
        """End every session, returning how many there were.

        The "sign out all devices" primitive, and the reason the cookie holds an
        id rather than the key: signing everyone out is this, not a credential
        change that would also break every script and probe.
        """
        count = len(self._tokens)
        self._tokens.clear()
        self._save()
        return count

    def __len__(self) -> int:
        return len(self._tokens)


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
