"""Rate limiting for sign-in attempts.

One shared key and no user accounts means guessing is the only attack on the
form, and nothing about the form slows it down: a key short enough for a person
to type is short enough to try repeatedly over a LAN. A delay per attempt is not
enough on its own either — a fixed `sleep` is defeated by firing requests in
parallel, because sleeping the handler does not stop the next request starting.
Counting is what works: attempts are refused outright once there have been too
many, whether or not the previous ones have finished.

**Keyed on the peer address, and forwarded headers are deliberately ignored.**
`X-Forwarded-For` is set by whoever is closest to the server, so behind a proxy
it is attacker-controlled — a guesser rotating that header would get a fresh
allowance for every attempt, which is worse than no limit at all because it
looks like one. The cost is that behind a proxy every request shares the proxy's
address and a lockout is global rather than per-client. That is the safe
direction to fail, and it is the same reasoning as the ingress peer check.

Nothing here reads configuration or touches aiohttp.
"""

from __future__ import annotations

import time

#: Failures before the door closes entirely.
LOCKOUT_AFTER = 5

#: How long it stays closed. Long enough to make guessing pointless, short
#: enough that locking yourself out is an annoyance rather than an outage.
LOCKOUT_SECONDS = 15 * 60

#: The wait after a first failure, doubling with each one after it. A typo
#: costs a second; a script costs progressively more, well before the lockout.
BACKOFF_BASE_SECONDS = 1.0

#: How long an address has to be quiet before its failures are forgotten.
#:
#: Deliberately far longer than the backoff. Dropping an entry as soon as its
#: wait elapsed would make the count reachable only by attempts made *while
#: already waiting* — so a script that simply paused between guesses would start
#: every one with a clean record and never reach the lockout at all. Counting
#: has to outlive the wait it produced, or the lockout only ever catches the
#: impatient.
FORGET_AFTER_SECONDS = LOCKOUT_SECONDS


class LoginThrottle:
    """Failed attempts per address, and how long each must now wait.

    One instance serves the running server; tests build their own rather than
    reaching into shared state to reset it.
    """

    def __init__(
        self,
        lockout_after: int = LOCKOUT_AFTER,
        lockout_seconds: float = LOCKOUT_SECONDS,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        forget_after: float = FORGET_AFTER_SECONDS,
    ) -> None:
        self._lockout_after = lockout_after
        self._lockout_seconds = lockout_seconds
        self._backoff_base = backoff_base
        self._forget_after = forget_after
        #: address → (failures, when the next attempt is allowed, when it last failed)
        self._failures: dict[str, tuple[int, float, float]] = {}

    def retry_after(self, address: str, now: float | None = None) -> float:
        """Seconds this address must wait, or 0.0 if it may try now.

        Read before checking the key, so a locked-out caller is refused without
        the comparison happening at all.
        """
        record = self._failures.get(address)
        if record is None:
            return 0.0
        _, allowed_at, _ = record
        moment = time.time() if now is None else now
        return max(0.0, allowed_at - moment)

    def record_failure(self, address: str, now: float | None = None) -> None:
        """Count a wrong key and push this address's next attempt further out."""
        moment = time.time() if now is None else now
        failures = self._failures.get(address, (0, 0.0, 0.0))[0] + 1
        if failures >= self._lockout_after:
            wait = self._lockout_seconds
        else:
            wait = self._backoff_base * (2 ** (failures - 1))
        self._failures[address] = (failures, moment + wait, moment)

    def record_success(self, address: str) -> None:
        """Forget an address's failures.

        Someone who signs in has proved they are not guessing, and leaving the
        count behind would make the next mistyped key cost more than it should.
        """
        self._failures.pop(address, None)

    def prune(self, now: float | None = None) -> None:
        """Forget addresses that have been quiet since well before the lockout.

        The dict is keyed by address, so without this a guesser walking a subnet
        leaves an entry per address for as long as the process runs. Called from
        the login path rather than a timer: the only thing that grows this is a
        sign-in attempt, so that is where it is worth tidying.

        Keyed on the last failure rather than on the wait it produced. Forgetting
        as soon as the wait elapsed would hand a patient script a clean record
        before every attempt, and the lockout would then only ever catch someone
        guessing faster than the backoff.
        """
        moment = time.time() if now is None else now
        stale = [
            addr
            for addr, (_, _, last_failure) in self._failures.items()
            if moment - last_failure >= self._forget_after
        ]
        for address in stale:
            del self._failures[address]

    def __len__(self) -> int:
        return len(self._failures)


#: The running server's failed sign-in attempts.
throttle = LoginThrottle()
