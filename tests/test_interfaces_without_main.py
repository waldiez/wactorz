"""Which chat interfaces run when no main does, as in the minimal profile.

The social channels answer through the orchestrator alone, so they run with
or without main. The command line and the REST interface answer lifecycle
commands through main itself, so without it the run is headless.
"""

import argparse
import sys
from typing import Any, cast

import pytest

from wactorz import app as app_module
from wactorz.interfaces import chat_interfaces
from wactorz.orchestration import Orchestrator


class _System:
    """Stands in for the actor system: records that it ran."""

    def __init__(self) -> None:
        self._running = False
        self.ran = False

    async def run_forever(self) -> None:
        self.ran = True


class _Interface:
    """Stands in for any chat interface: records in ``started`` that it ran."""

    def __init__(self, name: str, started: list[str]) -> None:
        self.name = name
        self.started = started

    async def run(self) -> None:
        self.started.append(self.name)


@pytest.fixture(name="started")
def started_fixture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every interface `_run_interface` can build, recording which of them run."""
    started: list[str] = []
    for name in ("CLIInterface", "RESTInterface", "TelegramInterface"):
        monkeypatch.setattr(
            chat_interfaces, name, lambda *_a, _name=name, **_k: _Interface(_name, started)
        )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    return started


async def _run(interface: str, *, main: object | None, companions: list[Any]) -> _System:
    system = _System()
    args = argparse.Namespace(port=None, telegram_token="a-token", telegram_allowed_user_id=None)
    await app_module._run_interface(
        args,
        cast(Any, system),
        main,
        cast(Orchestrator, object()),
        interface,
        companions,
    )
    return system


async def test_a_social_primary_runs_without_main(started: list[str]) -> None:
    system = await _run("telegram", main=None, companions=[])

    assert started == ["TelegramInterface"]
    assert system.ran


async def test_companions_run_without_main(started: list[str]) -> None:
    system = await _run(app_module.HEADLESS, main=None, companions=[_Interface("discord", started)])

    assert started == ["discord"]
    assert system.ran


@pytest.mark.parametrize("interface", sorted(app_module.MAIN_INTERFACES))
async def test_an_interface_that_needs_main_is_left_out_and_companions_still_run(
    started: list[str], interface: str
) -> None:
    system = await _run(interface, main=None, companions=[_Interface("discord", started)])

    assert started == ["discord"]
    assert system.ran


async def test_with_main_the_command_line_runs(started: list[str]) -> None:
    await _run("cli", main=object(), companions=[])

    assert started == ["CLIInterface"]
