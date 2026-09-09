"""Retry, backoff and per-attempt timeout around every LLM provider call."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import _patch, patch

import pytest

from tests.optional_deps import ensure_importable

ensure_importable("anthropic")

from wactorz.agents.llm.base import LLMProvider, ToolCompletion
from wactorz.agents.llm.providers import anthropic as anthropic_provider
from wactorz.agents.llm.providers.anthropic import AnthropicProvider
from wactorz.agents.llm.retry import (
    BACKOFF_BASE_S,
    RETRY_AFTER_CAP_S,
    RETRYABLE_STATUS,
    ProviderUnavailable,
    attempt_timeout,
    backoff_delay,
    call_with_retry,
    is_retryable,
    retry_after_seconds,
    retry_delay,
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

    def __init__(self, status_code: int, headers: "Headers | None" = None) -> None:
        self.status_code = status_code
        if headers is not None:
            self.headers = headers


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

    with _with_policy(retries=2), pytest.raises(ProviderUnavailable) as caught:
        await call_with_retry(operation, "test")
    assert calls == 3
    assert isinstance(caught.value.__cause__, Boom)


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

    # Still wrapped: one attempt that failed transiently is still exhaustion,
    # and the original failure is kept as the cause.
    with _with_policy(retries=0), pytest.raises(ProviderUnavailable) as caught:
        await call_with_retry(operation, "test")
    assert calls == 1
    assert isinstance(caught.value.__cause__, Boom)


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


# ── Retry-After: the provider knows when its limit resets, we are guessing ───


class Headers:
    """A case-insensitive header mapping, as both SDK shapes provide."""

    def __init__(self, **items: str) -> None:
        self._items = {k.lower().replace("_", "-"): v for k, v in items.items()}

    def get(self, key: str) -> str | None:
        return self._items.get(key.lower())


def test_retry_after_read_from_both_sdk_shapes() -> None:
    assert retry_after_seconds(Boom(headers=Headers(retry_after="3"))) == 3.0  # aiohttp
    assert retry_after_seconds(Boom(response=Response(429, Headers(retry_after="7")))) == 7.0
    assert retry_after_seconds(Boom(status_code=429)) is None  # no header at all


def test_retry_after_accepts_an_http_date() -> None:
    """RFC 9110 allows a date as well as a delta, and both appear in the wild."""
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    stamp = format_datetime(soon)
    seconds = retry_after_seconds(Boom(headers=Headers(retry_after=stamp)))
    assert seconds is not None
    assert 25 <= seconds <= 31


def test_retry_after_wins_over_our_backoff() -> None:
    """The point of honouring it: we do not override the provider's own number."""
    delay = retry_delay(Boom(status_code=429, headers=Headers(retry_after="4")), attempt=0)
    assert delay == 4.0  # not the <=0.5s backoff attempt 0 would have produced


def test_backoff_is_used_when_no_retry_after_is_sent() -> None:
    delay = retry_delay(Boom(status_code=429), attempt=0)
    assert delay is not None
    assert 0 <= delay <= BACKOFF_BASE_S


def test_an_unreasonable_retry_after_stops_rather_than_holding_the_agent() -> None:
    long_wait = str(int(RETRY_AFTER_CAP_S) + 1)
    assert retry_delay(Boom(status_code=429, headers=Headers(retry_after=long_wait)), 0) is None


async def test_a_long_retry_after_fails_fast_instead_of_sleeping(no_sleep: None) -> None:
    calls = 0

    async def rate_limited() -> str:
        nonlocal calls
        calls += 1
        raise Boom(status_code=429, headers=Headers(retry_after="86400"))

    with _with_policy(retries=5), pytest.raises(ProviderUnavailable):
        await call_with_retry(rate_limited, "test")
    assert calls == 1, "waited on, or retried past, an hours-long Retry-After"


# ── exhaustion reads differently from a bad request ──────────────────────────


async def test_exhaustion_is_a_distinct_error_naming_the_provider(no_sleep: None) -> None:
    async def rate_limited() -> str:
        raise Boom(status_code=429)

    with _with_policy(retries=2), pytest.raises(ProviderUnavailable) as caught:
        await call_with_retry(rate_limited, "AnthropicProvider.complete")

    message = str(caught.value)
    assert "rate-limiting" in message
    assert "not a problem with the request" in message
    assert caught.value.attempts == 3
    assert isinstance(caught.value.__cause__, Boom), "the underlying failure was lost"


async def test_a_bad_request_is_not_dressed_up_as_unavailability(no_sleep: None) -> None:
    """A 400 must reach the caller as itself, not as ProviderUnavailable."""

    async def malformed() -> str:
        raise Boom(status_code=400)

    with _with_policy(retries=2), pytest.raises(Boom):
        await call_with_retry(malformed, "test")


async def test_a_stream_that_never_starts_reports_unavailability(no_sleep: None) -> None:
    async def rate_limited() -> AsyncIterator[str]:
        raise Boom(status_code=429)
        yield  # pragma: no cover - makes this an async generator

    with _with_policy(retries=1), pytest.raises(ProviderUnavailable):
        _ = [c async for c in stream_with_retry(rate_limited, "test")]


# ── transient retry must not fold into parameter negotiation ─────────────────
#
# AnthropicProvider._create already retries a 400 once, having dropped whatever
# the model refused (`_degrade`). That lives below `_complete`, so it is inside
# this module's wrapper. The two must stay separate: a genuine 400 turned into
# three identical calls is the failure its own comments warn about.


class FakeMessages:
    """Records requests, and fails the first N with a given error."""

    def __init__(self, fail_first: int = 0, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.error = error

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_first and self.error is not None:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def anthropic_with(messages: FakeMessages) -> AnthropicProvider:
    """A provider wired to a fake client, without touching the anthropic SDK."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "claude-sonnet-4-6"
    provider.client = SimpleNamespace(messages=messages)  # pyright: ignore[reportAttributeAccessIssue]
    return provider


class BadRequest(Exception):
    """Stands in for anthropic.BadRequestError, which needs an httpx response."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = 400


TEMPERATURE_REFUSED = (
    "Error code: 400 - {'error': {'message': '`temperature` is deprecated for this model.'}}"
)


@pytest.fixture(name="forget_learned_models", autouse=True)
def forget_learned_models_fixture() -> None:
    """What one test teaches the provider must not leak into the next."""
    anthropic_provider._learned_no_sampling.clear()
    anthropic_provider._learned_no_thinking_param.clear()
    anthropic_provider._learned_no_effort.clear()


async def test_parameter_negotiation_still_works_under_the_retry_wrapper(
    no_sleep: None,
) -> None:
    """The 400-and-drop-the-parameter path is untouched: two calls, one answer."""
    messages = FakeMessages(fail_first=1, error=BadRequest(TEMPERATURE_REFUSED))
    provider = anthropic_with(messages)

    with (
        _with_policy(retries=2),
        patch("wactorz.config.CONFIG", replace(CONFIG, llm_temperature=0.0)),
    ):
        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])

    assert text == "ok"
    assert len(messages.calls) == 2, "negotiation no longer retries, or it retried too often"
    assert "temperature" in messages.calls[0]
    assert "temperature" not in messages.calls[1], "the refused parameter was not dropped"


async def test_an_unfixable_400_is_not_tripled(no_sleep: None) -> None:
    """The trap: transient retry stacking on negotiation would make one bad
    request three identical calls."""
    messages = FakeMessages(fail_first=99, error=BadRequest("Error code: 400 - malformed"))
    provider = anthropic_with(messages)

    with _with_policy(retries=2), pytest.raises(BadRequest):
        await provider.complete([{"role": "user", "content": "hi"}])

    assert len(messages.calls) == 1, f"a 400 was retried: {len(messages.calls)} calls"


async def test_a_429_from_anthropic_is_retried_not_degraded(no_sleep: None) -> None:
    """The other half: a rate limit is ours to retry, and must not be mistaken
    for a parameter the model refused."""
    messages = FakeMessages(fail_first=1, error=Boom(status_code=429))
    provider = anthropic_with(messages)

    with _with_policy(retries=2):
        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])

    assert text == "ok"
    assert len(messages.calls) == 2
    # Degradation drops a parameter; a retry must send the same request again.
    assert messages.calls[0].keys() == messages.calls[1].keys()


# ── the spend cap is not tripped by attempts that were never billed ──────────


async def test_a_retry_storm_checks_the_spend_cap_once(no_sleep: None) -> None:
    """The cap is a spend guard, not a call counter.

    Checking it per attempt would make a rate-limit storm look like a spend
    event and could trip the cap on traffic that produced no answer and no bill.
    """
    checks = 0

    def counting_check() -> None:
        nonlocal checks
        checks += 1

    provider = FlakyProvider(failures=3)
    with (
        patch("wactorz.agents.llm.base.check_cost_limit", counting_check),
        _with_policy(retries=3),
    ):
        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])

    assert text == "ok"
    assert provider.attempts == 4, "the retries did not happen"
    assert checks == 1, "the spend cap was checked per attempt rather than per call"


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

    with _with_policy(retries=0, timeout=0.01), pytest.raises(ProviderUnavailable) as caught:
        await call_with_retry(hangs, "test")
    assert started.is_set()
    assert cancelled, "the abandoned request was left running"
    assert isinstance(caught.value.__cause__, asyncio.TimeoutError)


async def test_a_stream_that_never_starts_is_abandoned() -> None:
    """The wait for a first chunk is bounded; the rest of the stream is not."""

    async def never_starts() -> AsyncIterator[str]:
        await asyncio.sleep(60)
        yield "never"

    with _with_policy(retries=0, timeout=0.01), pytest.raises(ProviderUnavailable) as caught:
        _ = [c async for c in stream_with_retry(never_starts, "test")]
    assert isinstance(caught.value.__cause__, asyncio.TimeoutError)


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
