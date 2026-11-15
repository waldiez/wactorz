# SPDX-License-Identifier: Apache-2.0
"""A companion chat channel that fails stops itself, not the server.

Companions (the Telegram and Discord bots next to the dashboard) are gathered
with the actor system and the primary interface. An exception out of one would
end that gather, and the server with it: the dashboard, the REST API and every
agent, over a bot that could not log in. These tests pin that a failing
companion is logged and left behind while everything else runs on.
"""

import argparse
import asyncio
import logging
from typing import Any, cast

import pytest

from wactorz import app as app_module
from wactorz.interfaces import chat_interfaces
from wactorz.interfaces.chat_interfaces import run_all_interfaces, run_companion
from wactorz.orchestration import Orchestrator


class _Broken:
    """A companion that cannot come up."""

    async def run(self) -> None:
        raise ConnectionError("api.telegram.org unreachable")


class _Healthy:
    """A companion that keeps going a little after the broken one has failed."""

    def __init__(self) -> None:
        self.finished = False

    async def run(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        self.finished = True


class _Forever:
    """A companion that runs until cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class _System:
    """Stands in for the actor system: records that it ran."""

    def __init__(self) -> None:
        self._running = False
        self.ran = False

    async def run_forever(self) -> None:
        self.ran = True


class _Rest:
    """Stands in for the REST interface: records that it ran."""

    def __init__(self) -> None:
        self.ran = False

    async def run(self) -> None:
        self.ran = True


async def test_a_failing_companion_is_logged_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        await run_companion(_Broken())

    assert "_Broken stopped" in caplog.text
    assert "keep running" in caplog.text


async def test_cancelling_a_companion_still_cancels_it() -> None:
    companion = _Forever()
    task = asyncio.create_task(run_companion(companion))
    await companion.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_other_companions_run_on() -> None:
    healthy = _Healthy()

    await asyncio.gather(*run_all_interfaces([_Broken(), healthy]))

    assert healthy.finished


async def test_the_server_keeps_running_past_a_companion_that_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest = _Rest()
    monkeypatch.setattr(chat_interfaces, "RESTInterface", lambda *_a, **_k: rest)
    system = _System()
    healthy = _Healthy()
    args = argparse.Namespace(port=None)

    # No exception out of here: an exception is what `_run` reports as
    # "System error" before it shuts the whole system down.
    await app_module._run_interface(
        args,
        cast(Any, system),
        object(),
        cast(Orchestrator, object()),
        "rest",
        [_Broken(), healthy],
    )

    assert rest.ran
    assert system.ran
    assert healthy.finished
