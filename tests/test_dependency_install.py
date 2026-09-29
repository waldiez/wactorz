"""What an agent's install reports, and what a caller waiting on it is told.

The outcome decides whether an agent is started at all, so every way an install
can end has its own message: a package that failed, an install that needs a
restart, an installer that never answered, and none to ask.
"""

import asyncio
from typing import Any

import pytest

from wactorz.agents import dependency_install
from wactorz.agents.dependency_install import InstallOutcome, install_for_agent, outcome_from_result


class _Installer:
    """Takes the request and never answers it."""

    name = "installer"
    actor_id = "installer-id"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def receive(self, msg: Any) -> None:
        self.requests.append(msg)


class _Registry:
    def __init__(self, installer: _Installer | None) -> None:
        self._installer = installer

    def find_by_name(self, name: str) -> Any:
        return self._installer if name == "installer" else None


class _Requester:
    name = "catalog"
    actor_id = "catalog-id"

    def __init__(self, installer: _Installer | None) -> None:
        self._registry = _Registry(installer)


class _Main:
    actor_id = "main-id"

    def __init__(self) -> None:
        self._result_futures: dict[str, asyncio.Future[Any]] = {}


class TestWhatThePersonIsTold:
    def test_a_ready_install_has_nothing_to_say(self) -> None:
        assert InstallOutcome().ok
        assert InstallOutcome().problem("reachy-mini") == ""

    def test_an_install_that_ran_out_of_time_says_what_was_kept(self) -> None:
        problem = InstallOutcome(timed_out=True).problem("reachy-mini")

        assert "did not finish in time" in problem
        assert "anything already installed is kept" in problem

    def test_no_installer_says_so(self) -> None:
        assert "installer agent is not running" in InstallOutcome(unavailable=True).problem("x")

    def test_a_result_that_is_not_a_dict_reads_as_nothing_wrong(self) -> None:
        assert outcome_from_result("installed").ok


class TestWaitingForTheInstaller:
    async def test_an_installer_that_never_answers_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installer, main = _Installer(), _Main()
        monkeypatch.setattr(dependency_install, "install_wait_s", lambda _count: 0.05)

        outcome = await install_for_agent(_Requester(installer), main, ["numpy"], "x")

        assert outcome.timed_out
        assert installer.requests[0].payload["notify"] is True
        assert main._result_futures == {}  # the pending reply slot is cleaned up

    async def test_without_an_installer_nothing_is_attempted(self) -> None:
        outcome = await install_for_agent(_Requester(None), _Main(), ["numpy"], "x")

        assert outcome.unavailable
