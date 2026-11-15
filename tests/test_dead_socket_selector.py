# SPDX-License-Identifier: Apache-2.0
"""One socket closed under the event loop costs its owner a connection, not the process.

On Windows the selector loop waits in ``select()``, which fails outright when any
handle it is given is no longer a socket. Uncaught, that ended the loop and every
agent with it. The selector Wactorz installs reports such a socket as ready, so
its owner's read fails and the owner cleans up and reconnects as for any dropped
connection, and stops watching it if the owner does not.

The failure is Windows' own; these tests stand ``select()`` in for it, so they
run anywhere.
"""

import asyncio
import errno
import selectors
import socket
import sys
from collections.abc import Iterator

import pytest

from wactorz._bootstrap import new_selector_loop
from wactorz._selector import NOT_A_SOCKET, DeadSocketTolerantSelector


def _not_a_socket() -> OSError:
    error = OSError(errno.EBADF, "An operation was attempted on something that is not a socket")
    if getattr(error, "winerror", None) != NOT_A_SOCKET:
        error.winerror = NOT_A_SOCKET  # pyright: ignore[reportAttributeAccessIssue]  # set by Windows only
    return error


@pytest.fixture(name="pair")
def pair_fixture() -> Iterator[tuple[socket.socket, socket.socket]]:
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


def _selector(dead: set[int], monkeypatch: pytest.MonkeyPatch) -> DeadSocketTolerantSelector:
    """A selector whose ``select()`` fails while any of ``dead`` is still watched."""
    selector = DeadSocketTolerantSelector(probe=lambda fd: fd in dead)

    def _select(self, r, w, x, timeout=None):  # noqa: ANN202
        if dead & (set(r) | set(w)):
            raise _not_a_socket()
        return [], [], []

    monkeypatch.setattr(selectors.SelectSelector, "_select", _select)
    return selector


class TestADeadSocket:
    def test_it_is_handed_back_to_its_owner_as_ready(
        self, pair: tuple[socket.socket, socket.socket], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live, gone = pair
        selector = _selector({gone.fileno()}, monkeypatch)
        selector.register(live, selectors.EVENT_READ, "live")
        selector.register(gone, selectors.EVENT_READ, "gone")

        ready = selector.select(0)

        assert [key.data for key, _ in ready] == ["gone"]

    def test_one_left_registered_is_no_longer_watched_the_second_time(
        self, pair: tuple[socket.socket, socket.socket], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live, gone = pair
        selector = _selector({gone.fileno()}, monkeypatch)
        selector.register(live, selectors.EVENT_READ, "live")
        selector.register(gone, selectors.EVENT_READ, "gone")

        selector.select(0)  # handed back; this owner does nothing about it
        ready = selector.select(0)

        assert ready == []
        assert [key.data for key in selector.get_map().values()] == ["live"]
        assert selector.select(0) == []

    def test_a_writer_is_handed_back_too(
        self, pair: tuple[socket.socket, socket.socket], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, gone = pair
        selector = _selector({gone.fileno()}, monkeypatch)
        selector.register(gone, selectors.EVENT_WRITE, "gone")

        (key, events), *_ = selector.select(0)

        assert key.data == "gone"
        assert events == selectors.EVENT_WRITE


class TestOtherFailures:
    def test_any_other_error_still_raises(
        self, pair: tuple[socket.socket, socket.socket], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live, _ = pair
        selector = DeadSocketTolerantSelector(probe=lambda _fd: True)

        def _select(self, r, w, x, timeout=None):  # noqa: ANN202
            raise OSError(errno.EINVAL, "something else")

        monkeypatch.setattr(selectors.SelectSelector, "_select", _select)
        selector.register(live, selectors.EVENT_READ)

        with pytest.raises(OSError, match="something else"):
            selector.select(0)

    def test_not_a_socket_with_every_handle_alive_still_raises(
        self, pair: tuple[socket.socket, socket.socket], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live, _ = pair
        selector = DeadSocketTolerantSelector(probe=lambda _fd: False)

        def _select(self, r, w, x, timeout=None):  # noqa: ANN202
            raise _not_a_socket()

        monkeypatch.setattr(selectors.SelectSelector, "_select", _select)
        selector.register(live, selectors.EVENT_READ)

        with pytest.raises(OSError):
            selector.select(0)


class TestTheLoop:
    def test_it_is_a_selector_loop_over_the_tolerant_selector(self) -> None:
        loop = new_selector_loop()
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)
            assert isinstance(loop._selector, DeadSocketTolerantSelector)  # pyright: ignore[reportAttributeAccessIssue]
        finally:
            loop.close()

    @pytest.mark.skipif(sys.platform != "win32", reason="the policy is installed on Windows only")
    def test_windows_loops_use_it(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            assert isinstance(loop._selector, DeadSocketTolerantSelector)  # pyright: ignore[reportAttributeAccessIssue]
        finally:
            loop.close()
