"""Retry, backoff and per-attempt timeout around every LLM provider call."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import _patch, patch

import pytest

from wactorz.agents.llm.base import LLMProvider, ToolCompletion
from wactorz.agents.llm.retry import (
    RETRYABLE_STATUS,
    attempt_timeout,
    backoff_delay,
    call_with_retry,
    is_retryable,
    status_of,
    stream_with_retry,
)
from wactorz.config import CONFIG


def _with_policy(retries: int = 2, timeout: float = 300.0) -> _patch:
    """CONFIG patched to a given retry/timeout policy."""
    return patch(
        "wactorz.config.CONFIG",
        replace(CONFIG, llm_max_retries=retries, llm_timeout_s=timeout),
    )


class Boom(Exception):
    """A provider failure carrying whatever attribute an SDK would carry."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__("boom")
        for name, value in attrs.items():
            setattr(self, name, value)


class Response:
    """Stands in for the `.response` an httpx-backed SDK hangs off its errors."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture(name="no_sleep")
def no_sleep_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the backoff so a retry test costs no wall time.

    Zeroes the two constants rather than replacing `asyncio.sleep`: that name is
    the real module's, so patching it would silently un-hang the sleeps a test
    uses to *simulate* a stalled provider.
    """
    monkeypatch.setattr("wactorz.agents.llm.retry.BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr("wactorz.agents.llm.retry.BACKOFF_CAP_S", 0.0)


# ── which failures are worth repeating ───────────────────────────────────────


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_transient_statuses_retry(status: int) -> None:
    assert is_retryable(Boom(status_code=status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_final(status: int) -> None:
    assert not is_retryable(Boom(status_code=status))


def test_status_read_from_each_sdk_spelling() -> None:
    assert status_of(Boom(status_code=429)) == 429  # openai / anthropic
    assert status_of(Boom(status=503)) == 503  # aiohttp
    assert status_of(Boom(code=500)) == 500  # google-genai
    assert status_of(Boom(response=Response(429))) == 429  # httpx-shaped
    assert status_of(Boom()) is None


def test_transport_failures_retry_without_a_status() -> None:
    assert is_retryable(asyncio.TimeoutError())
    assert is_retryable(TimeoutError())
    assert is_retryable(ConnectionResetError())
    assert is_retryable(OSError("connection refused"))


def test_a_failure_with_no_status_and_no_transport_cause_is_final() -> None:
    assert not is_retryable(ValueError("bad request shape"))


def test_backoff_grows_and_is_capped() -> None:
    # Full jitter, so only the ceiling is deterministic.
    assert all(0 <= backoff_delay(0) <= 0.5 for _ in range(20))
    assert all(0 <= backoff_delay(3) <= 4.0 for _ in range(20))
    assert all(0 <= backoff_delay(99) <= 30.0 for _ in range(20))


# ── the retry loop itself ────────────────────────────────────────────────────


async def test_retries_a_429_then_succeeds(no_sleep: None) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise Boom(status_code=429)
        return "ok"

    with _with_policy(retries=2):
        assert await call_with_retry(operation, "test") == "ok"
    assert calls == 3


async def test_gives_up_after_the_configured_retries(no_sleep: None) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise Boom(status_code=503)

    with _with_policy(retries=2), pytest.raises(Boom):
        await call_with_retry(operation, "test")
    assert calls == 3


async def test_a_final_error_is_not_retried(no_sleep: None) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise Boom(status_code=401)

    with _with_policy(retries=2), pytest.raises(Boom):
        await call_with_retry(operation, "test")
    assert calls == 1


async def test_zero_retries_makes_the_first_failure_the_answer(no_sleep: None) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise Boom(status_code=429)

    with _with_policy(retries=0), pytest.raises(Boom):
        await call_with_retry(operation, "test")
    assert calls == 1


async def test_a_hung_call_times_out_and_is_retried(no_sleep: None) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(60)
        return "ok"

    with _with_policy(retries=1, timeout=0.01):
        assert await call_with_retry(operation, "test") == "ok"
    assert calls == 2


# ── stopping an agent mid-call ───────────────────────────────────────────────


async def test_a_cancelled_call_stays_cancelled() -> None:
    """A cancellation arriving as the reply lands must not be absorbed.

    `asyncio.wait_for` absorbs it on Python 3.10 — it returns the result from
    inside its own CancelledError handler when the guarded future is already
    done. An LLM call is the longest await an actor makes, so it is what a stop
    or a shutdown interrupts; swallowing that leaves the actor running past a
    stop it was told about. Unlike the task-based case in
    `test_cancellation_is_not_swallowed`, a bare future pins the window exactly:
    the result is set and the caller cancelled in the same loop turn.
    """
    reply = asyncio.get_running_loop().create_future()
    returned = False

    async def caller() -> None:
        nonlocal returned
        with _with_policy(retries=0, timeout=10.0):
            await call_with_retry(lambda: reply, "test")
        returned = True

    task = asyncio.ensure_future(caller())
    await asyncio.sleep(0)  # let it reach the await
    reply.set_result("the model answered")  # the reply lands...
    task.cancel()  # ...and the agent is stopped, same turn

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not returned, "the cancellation was swallowed and the caller ran on"


async def test_an_abandoned_attempt_does_not_keep_running() -> None:
    """A timed-out request is cancelled, not left in flight."""
    started = asyncio.Event()
    cancelled = False

    async def hangs() -> str:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return "never"

    with _with_policy(retries=0, timeout=0.01), pytest.raises(asyncio.TimeoutError):
        await call_with_retry(hangs, "test")
    assert started.is_set()
    assert cancelled, "the abandoned request was left running"


async def test_a_stream_that_never_starts_is_abandoned() -> None:
    """The wait for a first chunk is bounded; the rest of the stream is not."""

    async def never_starts() -> AsyncIterator[str]:
        await asyncio.sleep(60)
        yield "never"

    with _with_policy(retries=0, timeout=0.01), pytest.raises(asyncio.TimeoutError):
        _ = [c async for c in stream_with_retry(never_starts, "test")]


async def test_a_slow_stream_is_not_cut_off_once_it_has_started() -> None:
    """Only time-to-first-chunk is bounded, so a long answer still completes."""

    async def slow_after_first() -> AsyncIterator[str]:
        yield "first"
        await asyncio.sleep(0.05)  # longer than the timeout below
        yield "second"

    with _with_policy(retries=0, timeout=0.02):
        chunks = [c async for c in stream_with_retry(slow_after_first, "test")]
    assert chunks == ["first", "second"]


async def test_an_empty_stream_ends_cleanly() -> None:
    async def empty() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - makes this an async generator

    with _with_policy(retries=0):
        assert not [c async for c in stream_with_retry(empty, "test")]


@pytest.mark.parametrize("configured", [0.0, -5.0])
async def test_a_non_positive_timeout_means_no_timeout(configured: float) -> None:
    """0 is the documented escape hatch; a negative value is a typo, and reading
    it literally would time every attempt out instantly."""
    with _with_policy(retries=0, timeout=configured):
        assert attempt_timeout() is None

        async def slower_than_zero() -> str:
            await asyncio.sleep(0.02)
            return "waited"

        assert await call_with_retry(slower_than_zero, "test") == "waited"


# ── every provider inherits it, through the public methods ───────────────────


class FlakyProvider(LLMProvider):
    """Fails with `failures` rate-limit errors before answering."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def _tick(self) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise Boom(status_code=429)

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        self._tick()
        return "ok", {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        self._tick()
        return ToolCompletion(content="ok", usage={"cost_usd": 0.0})

    async def _stream(self, messages: list[dict], system: str = "", **kwargs: Any):
        self._tick()
        yield "ok"


async def test_complete_retries_through_the_base_class(no_sleep: None) -> None:
    provider = FlakyProvider(failures=1)
    with _with_policy(retries=2):
        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])
    assert text == "ok"
    assert provider.attempts == 2


async def test_complete_with_tools_retries_through_the_base_class(no_sleep: None) -> None:
    provider = FlakyProvider(failures=1)
    with _with_policy(retries=2):
        result = await provider.complete_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert result.content == "ok"
    assert provider.attempts == 2


async def test_stream_retries_before_the_first_chunk(no_sleep: None) -> None:
    provider = FlakyProvider(failures=1)
    with _with_policy(retries=2):
        chunks = [c async for c in provider.stream([{"role": "user", "content": "hi"}])]
    assert chunks == ["ok"]
    assert provider.attempts == 2


async def test_stream_does_not_replay_what_the_reader_already_saw(no_sleep: None) -> None:
    """A mid-stream failure propagates: retrying would repeat delivered text."""
    attempts = 0

    class MidStreamFailure(LLMProvider):
        async def _stream(self, messages: list[dict], system: str = "", **kwargs: Any):
            nonlocal attempts
            attempts += 1
            yield "half a "
            raise Boom(status_code=503)

    provider = MidStreamFailure()
    seen: list[str] = []
    with _with_policy(retries=2), pytest.raises(Boom):
        async for chunk in provider.stream([{"role": "user", "content": "hi"}]):
            seen.append(chunk)
    assert seen == ["half a "]
    assert attempts == 1
