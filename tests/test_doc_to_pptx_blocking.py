"""External commands must not stop the rest of the process while they run.

``npm install`` here allows itself two minutes and ``node`` one, and both were
called inline from an ``async def``. Nothing else in the process ran for that
long — not other agents, not the broker read, and not the framework's own task
timeout, which could therefore never fire on the very calls it exists to bound.
"""

import asyncio
import subprocess
import time
from typing import Any

import pytest

from wactorz.catalogue_agents.doc_to_pptx_agent import AGENT_CODE

# The agent ships as source that the framework exec's when spawning it, so the
# only way to reach its helpers is the way the framework does.
_agent: dict = {}
exec(AGENT_CODE, _agent)
_run_blocking = _agent["_run_blocking"]


def _slow_command(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
    """Stands in for npm: blocks its caller for longer than a timeout allows."""
    time.sleep(0.3)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestRunBlocking:
    async def test_the_event_loop_keeps_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_agent["subprocess"], "run", _slow_command)

        ticks = 0

        async def _tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(_tick())
        try:
            await _run_blocking(["npm", "install"])
        finally:
            ticker.cancel()

        # Called inline this is 0: nothing else gets a turn until the command
        # returns, which is what let a 60s task timeout sit behind a 120s npm.
        assert ticks > 5

    async def test_a_task_timeout_can_now_fire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_agent["subprocess"], "run", _slow_command)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_run_blocking(["npm"]), timeout=0.05)

    async def test_the_result_is_passed_back_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = subprocess.CompletedProcess(
            args=["node"], returncode=1, stdout="out", stderr="e"
        )
        monkeypatch.setattr(_agent["subprocess"], "run", lambda *a, **k: expected)

        assert await _run_blocking(["node", "--version"]) is expected

    async def test_arguments_reach_subprocess_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def _capture(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(_agent["subprocess"], "run", _capture)
        await _run_blocking(["npm", "install"], cwd="/w", timeout=120)

        assert seen["cmd"] == ["npm", "install"]
        assert seen["kwargs"] == {"cwd": "/w", "timeout": 120}
