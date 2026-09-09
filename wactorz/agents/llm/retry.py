"""Retry, backoff and a per-attempt timeout for LLM requests.

Applied in ``LLMProvider``'s public methods, so every provider inherits this the
way it already inherits the spend check, and a new backend needs no retry code
of its own.

Deliberately provider-agnostic: nothing here imports an SDK. The HTTP status is
read off whatever the SDK raised by attribute name -- ``status_code`` on the
OpenAI and Anthropic clients, ``status`` on aiohttp's ``ClientResponseError``,
``code`` on google-genai's ``APIError``. An exception carrying none of them and
no transport failure is treated as final, which is the safe answer: a malformed
request retried three times is three times the latency for the same error.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Statuses that mean "later", not "no". 409 is here because both the OpenAI and
#: Anthropic clients use it for a transient lock conflict rather than a conflict
#: the caller could resolve by changing the request.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 30.0

#: Longest ``Retry-After`` worth waiting out. A provider asking for more than
#: this is not offering a pause, it is asking us to hold an actor hostage —
#: which is the failure this module exists to prevent — so we stop instead.
RETRY_AFTER_CAP_S = 60.0


class ProviderUnavailable(Exception):
    """Every attempt failed on something transient.

    Distinct from the provider's own error so a caller — and the person reading
    the message — can tell "the provider is busy, this may work later" from
    "the request was wrong and will fail again". The failure that caused it is
    kept as the exception's cause.
    """

    def __init__(self, label: str, attempts: int, last: BaseException) -> None:
        status = status_of(last)
        detail = f"HTTP {status}" if status is not None else type(last).__name__
        super().__init__(
            f"{label} gave up after {attempts} attempt(s): the provider kept "
            f"failing with {detail}. This is the provider being unavailable or "
            f"rate-limiting us, not a problem with the request."
        )
        self.label = label
        self.attempts = attempts
        self.last = last


def status_of(exc: BaseException) -> int | None:
    """The HTTP status an SDK exception carries, or None if it carries none."""
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    A status decides on its own when there is one: the service answered, and the
    answer says whether the request itself was the problem. Without one the
    failure never reached the service, so a timeout or any transport error is
    worth repeating.
    """
    status = status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError))


def max_attempts() -> int:
    """Total attempts for one call: the first, plus ``LLM_MAX_RETRIES``."""
    # Read per call, not bound at import: a module-level `from ... import CONFIG`
    # captures the object that existed at import time, so a later reconfiguration
    # would never be seen here. `_resolve_temperature` reads it the same way.
    from ...config import CONFIG

    return max(1, CONFIG.llm_max_retries + 1)


def attempt_timeout() -> float | None:
    """Seconds one attempt may take, or None to wait on the provider's own bound.

    Anything at or below zero means None. Zero is the escape hatch for a model
    that legitimately thinks for longer than any timeout worth setting; a
    negative value is a typo, and reading it literally would time every attempt
    out instantly and fail every call in the system.
    """
    from ...config import CONFIG

    timeout = CONFIG.llm_timeout_s
    return timeout if timeout > 0 else None


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own ``Retry-After``, in seconds, if it sent one.

    Read by attribute like the status is: ``headers`` on aiohttp's error,
    ``response.headers`` on the httpx-backed SDKs. RFC 9110 allows either a
    delta in seconds or an HTTP-date, and both appear in the wild.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        # Both spellings, because a plain dict is not case-insensitive the way
        # aiohttp's and httpx's header mappings are.
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def retry_delay(exc: BaseException, attempt: int) -> float | None:
    """Seconds to wait before the next attempt, or None to stop retrying.

    The provider's own ``Retry-After`` wins over our backoff when it sends one:
    it knows when its limit resets and we are guessing. Beyond
    ``RETRY_AFTER_CAP_S`` we stop rather than wait, because the caller gets a
    usable answer sooner from an error than from an actor held that long.
    """
    after = retry_after_seconds(exc)
    if after is None:
        return backoff_delay(attempt)
    return None if after > RETRY_AFTER_CAP_S else after


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter.

    Jittered because the actors in one process share a provider and hit the same
    rate limit together; retrying in lockstep would rebuild the burst that
    caused it.
    """
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))
    return random.uniform(0, ceiling)  # noqa: S311  # backoff jitter, not a secret


async def _pause(exc: BaseException, attempt: int, attempts: int, label: str, delay: float) -> None:
    """Log the failed attempt and wait out its delay."""
    # %r, not %s: an SDK's __str__ can reach for a field its own error object
    # never got (aiohttp's dereferences request_info), and a log line must not
    # be the thing that turns a retryable failure into a crash. repr names the
    # class and the constructor args, which is the part worth reading anyway.
    logger.warning(
        "[llm-retry] %s attempt %s of %s failed (%r) — retrying in %.1fs",
        label,
        attempt + 1,
        attempts,
        exc,
        delay,
    )
    await asyncio.sleep(delay)


async def _bounded(operation: Callable[[], Awaitable[T]], timeout: float | None) -> T:
    """One attempt, abandoned after ``timeout`` seconds.

    ``asyncio.wait``, not ``wait_for``, for the reason given in
    ``Actor._wind_down_tasks``: on Python 3.10 a ``wait_for`` whose guarded
    future completes in the same instant the caller is cancelled returns the
    result from inside its own ``CancelledError`` handler, and the caller never
    learns it was cancelled. An LLM call is the longest await an actor makes, so
    it is the one most likely to be in flight when a stop or a shutdown arrives
    — precisely the window that would be lost.
    """
    if timeout is None:
        return await operation()
    task = asyncio.ensure_future(operation())
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        # Cancelled while waiting: the request is ours, and asyncio.wait leaves
        # it running. Hand the cancellation on once it has been passed down.
        task.cancel()
        raise
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise asyncio.TimeoutError
    return task.result()


async def call_with_retry(operation: Callable[[], Awaitable[T]], label: str) -> T:
    """Await ``operation()`` under a timeout, retrying transient failures.

    ``operation`` is a factory rather than an awaitable because a coroutine can
    only be awaited once, and a retry needs a fresh one.
    """
    attempts = max_attempts()
    timeout = attempt_timeout()
    for attempt in range(attempts - 1):
        try:
            return await _bounded(operation, timeout)
        except Exception as exc:
            if not is_retryable(exc):
                raise
            delay = retry_delay(exc, attempt)
            if delay is None:
                raise ProviderUnavailable(label, attempt + 1, exc) from exc
            await _pause(exc, attempt, attempts, label, delay)
    # The last attempt has nothing left to wait for, so a transient failure here
    # is exhaustion rather than something to sleep on.
    try:
        return await _bounded(operation, timeout)
    except Exception as exc:
        if is_retryable(exc):
            raise ProviderUnavailable(label, attempts, exc) from exc
        raise


async def _timed_to_first_chunk(
    stream: AsyncIterator[Any], timeout: float | None
) -> AsyncIterator[Any]:
    """Yield from ``stream``, bounding only the wait for its first chunk.

    That wait is the one that can hang on a connection the service never
    answers, and it is the only part of a stream with a length worth predicting.
    The rest is not bounded here: how long the whole answer takes is the model's
    business, and the providers cap the gap *between* chunks themselves.
    """
    iterator = stream.__aiter__()
    try:
        first = await _bounded(iterator.__anext__, timeout)
    except StopAsyncIteration:
        return
    yield first
    async for chunk in iterator:
        yield chunk


async def stream_with_retry(
    operation: Callable[[], AsyncIterator[Any]], label: str
) -> AsyncIterator[Any]:
    """Yield from an LLM stream, retrying only while nothing has been yielded.

    Once a chunk has reached the caller the answer is partly delivered, and a
    second attempt would repeat text the reader has already seen — so a failure
    from that point on propagates.
    """
    attempts = max_attempts()
    timeout = attempt_timeout()
    for attempt in range(attempts - 1):
        started = False
        try:
            async for chunk in _timed_to_first_chunk(operation(), timeout):
                started = True
                yield chunk
        except Exception as exc:
            if started or not is_retryable(exc):
                raise
            delay = retry_delay(exc, attempt)
            if delay is None:
                raise ProviderUnavailable(label, attempt + 1, exc) from exc
            await _pause(exc, attempt, attempts, label, delay)
        else:
            return
    started = False
    try:
        async for chunk in _timed_to_first_chunk(operation(), timeout):
            started = True
            yield chunk
    except Exception as exc:
        if not started and is_retryable(exc):
            raise ProviderUnavailable(label, attempts, exc) from exc
        raise
