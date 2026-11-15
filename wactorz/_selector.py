# SPDX-License-Identifier: Apache-2.0
"""A ``select()`` selector that survives a socket closed out from under it.

Windows only uses it (see :mod:`wactorz._bootstrap`). There the selector event
loop waits in ``select()``, and ``select()`` fails outright -- ``WinError
10038``, "not a socket" -- when any one handle it is given has been closed. The
loop does not catch that, so a single dead socket, whoever owned it, ends the
loop and every agent, the dashboard and the broker connections with it. A
socket can die that way without its owner closing it: a handle closed twice
elsewhere in the process can be the number Windows has since reused for it.

This selector reports such a socket as ready instead. The loop then runs its
owner's callback, the owner's read fails as it would on a dropped connection,
and the owner cleans up and reconnects as it already does for one. A handle
still reported dead on a later call, because its owner did not unregister it,
is unregistered here, so a careless owner cannot keep the loop spinning.

Standard library only: the bootstrap imports this before anything else loads.
"""

import logging
import select
import selectors
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

#: ``WSAENOTSOCK``: what ``select()`` says when a handle it is given is no socket.
NOT_A_SOCKET = 10038


def _is_dead(fd: int) -> bool:
    """Whether ``select()`` refuses this one handle on its own."""
    try:
        select.select([fd], [], [], 0)
    except (OSError, ValueError):
        return True
    return False


def _not_a_socket(error: OSError) -> bool:
    return getattr(error, "winerror", None) == NOT_A_SOCKET


class DeadSocketTolerantSelector(selectors.SelectSelector):
    """``SelectSelector`` that hands a dead socket back to its owner instead of failing.

    ``probe`` tells whether a handle is dead; a test stands one in.
    """

    def __init__(self, probe: Callable[[int], bool] = _is_dead) -> None:
        super().__init__()
        self._probe = probe
        #: Handles reported dead on the last failed call, so a second report can
        #: be told apart from a first.
        self._reported: set[int] = set()

    def _select(
        self, r: Iterable[int], w: Iterable[int], x: Iterable[int], timeout: float | None = None
    ) -> tuple[list[int], list[int], list[int]]:
        try:
            ready = super()._select(r, w, x, timeout)  # pyright: ignore[reportAttributeAccessIssue]
        except OSError as error:
            if not _not_a_socket(error):
                raise
            return self._report_dead(list(r), list(w), error)
        self._reported.clear()
        return ready

    def _report_dead(
        self, readers: list[int], writers: list[int], error: OSError
    ) -> tuple[list[int], list[int], list[int]]:
        dead = {fd for fd in (*readers, *writers) if self._probe(fd)}
        if not dead:
            raise error
        again = dead & self._reported
        for fd in again:
            logger.warning(
                "[loop] Socket %s is still closed after its owner was told; no longer watching it",
                fd,
            )
            self.unregister(fd)
        fresh = dead - again
        self._reported = fresh
        for fd in fresh:
            logger.warning(
                "[loop] Socket %s was closed while the loop watched it; handing it back to its owner",
                fd,
            )
        return (
            [fd for fd in readers if fd in fresh],
            [fd for fd in writers if fd in fresh],
            [],
        )
