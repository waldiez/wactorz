"""GeminiProvider must not block the shared event loop.

Every actor in the process runs on one loop. A provider that calls the
*synchronous* google-genai surface from inside ``async def`` freezes all of
them for the whole round-trip — seconds, not milliseconds — which also starves
MQTT keepalive and can trip the 35 s heartbeat watchdog into force-restarting
healthy actors as "presumed crashed".

The fakes below expose *both* SDK surfaces: a sync ``client.models`` that really
blocks the thread, and an async ``client.aio.models`` that yields. So these
tests fail loudly while the provider uses the sync one, rather than silently
passing against a fake that never blocks.

No google-genai needed: the provider is built with ``__new__`` and handed the
attributes its methods use, so this runs on a machine without the extra.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import asyncio
import inspect
import time
import types
from collections.abc import Awaitable
from typing import Any

import pytest

from wactorz.agents.llm_agent import GeminiProvider

# Long enough that a blocked loop is unambiguous, short enough to stay fast.
_BLOCK = 0.2

TOOL = {
    "name": "get_data",
    "description": "Fetch data",
    "parameters": {"type": "object", "properties": {}},
}


class _Types:
    """Stand-in for google.genai.types — every factory just echoes kwargs."""

    @staticmethod
    def GenerateContentConfig(**kwargs: Any) -> dict[str, Any]:  # pylint: disable=invalid-name
        return kwargs

    @staticmethod
    def Tool(**kwargs: Any) -> dict[str, Any]:  # pylint: disable=invalid-name
        return kwargs

    @staticmethod
    def FunctionDeclaration(**kwargs: Any) -> dict[str, Any]:  # pylint: disable=invalid-name
        return kwargs


def _response() -> types.SimpleNamespace:
    """A reply carrying both plain text and a tool call, plus usage."""
    function_call = types.SimpleNamespace(id="call-1", name="get_data", args={})
    parts = [
        types.SimpleNamespace(text="hello", function_call=None),
        types.SimpleNamespace(text=None, function_call=function_call),
    ]
    return types.SimpleNamespace(
        text="hello",
        candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=parts))],
        usage_metadata=types.SimpleNamespace(prompt_token_count=11, candidates_token_count=7),
    )


class _SyncModels:
    """The blocking surface — mirrors the real sync SDK by stalling the thread."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        time.sleep(_BLOCK)
        return _response()


class _AsyncModels:
    """The awaitable surface (``client.aio.models``)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        await asyncio.sleep(_BLOCK)
        return _response()


def _provider() -> tuple[GeminiProvider, _SyncModels, _AsyncModels]:
    """A GeminiProvider whose client offers both surfaces."""
    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model_name = "gemini-2.5-flash"
    provider._types = _Types  # pyright: ignore[reportAttributeAccessIssue]
    sync_models, async_models = _SyncModels(), _AsyncModels()
    provider.client = types.SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        models=sync_models,
        aio=types.SimpleNamespace(models=async_models),
    )
    return provider, sync_models, async_models


async def _loop_ticks_during(coro: Awaitable) -> int:
    """Run `coro`, counting how many times an unrelated task got scheduled.

    A responsive loop lets the ticker run many times; a blocked one starves it.
    """
    ticks: list[int] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks.append(1)

    task = asyncio.create_task(_ticker())
    await asyncio.sleep(0)  # let the ticker reach its first await
    try:
        await coro
    finally:
        stop.set()
        await task
    return len(ticks)


# ── the actual requirement ──────────────────────────────────────────────────


async def test_complete_does_not_block_the_event_loop() -> None:
    provider, _sync, _async = _provider()
    ticks = await _loop_ticks_during(provider.complete([{"role": "user", "content": "hi"}]))
    assert ticks >= 5, f"event loop stalled during complete(); ticker ran only {ticks}x"


async def test_complete_with_tools_does_not_block_the_event_loop() -> None:
    # The common path once an agent has tools bound.
    provider, _sync, _async = _provider()
    ticks = await _loop_ticks_during(
        provider.complete_with_tools([{"role": "user", "content": "hi"}], tools=[TOOL])
    )
    assert ticks >= 5, f"event loop stalled during complete_with_tools(); ticker ran only {ticks}x"


# ── which surface is used ───────────────────────────────────────────────────


async def test_complete_uses_the_async_surface() -> None:
    provider, sync_models, async_models = _provider()
    await provider.complete([{"role": "user", "content": "hi"}])
    assert len(async_models.calls) == 1
    assert not sync_models.calls, "the synchronous SDK surface must not be used"


async def test_complete_with_tools_uses_the_async_surface() -> None:
    provider, sync_models, async_models = _provider()
    await provider.complete_with_tools([{"role": "user", "content": "hi"}], tools=[TOOL])
    assert len(async_models.calls) == 1
    assert not sync_models.calls, "the synchronous SDK surface must not be used"


# ── the response handling must survive the change ───────────────────────────


async def test_complete_still_reports_text_and_usage() -> None:
    provider, _sync, _async = _provider()
    text, usage = await provider.complete([{"role": "user", "content": "hi"}])
    assert text == "hello"
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 7
    assert usage["cost_usd"] > 0


async def test_complete_with_tools_still_parses_calls_and_usage() -> None:
    provider, _sync, _async = _provider()
    result = await provider.complete_with_tools([{"role": "user", "content": "hi"}], tools=[TOOL])
    assert result.content == "hello"
    assert [c.name for c in result.tool_calls] == ["get_data"]
    assert result.usage["input_tokens"] == 11
    assert result.usage["output_tokens"] == 7


async def test_the_system_prompt_and_tools_still_reach_the_sdk() -> None:
    provider, _sync, async_models = _provider()
    await provider.complete_with_tools(
        [{"role": "user", "content": "hi"}], tools=[TOOL], system="Be brief."
    )
    config = async_models.calls[0]["config"]
    assert config["system_instruction"] == "Be brief."
    assert config["tools"][0]["function_declarations"][0]["name"] == "get_data"


@pytest.mark.parametrize("method", ["complete", "complete_with_tools"])
def test_the_provider_methods_are_coroutines(method: str) -> None:
    # A sync def here would push the blocking call onto whichever thread called it.
    assert inspect.iscoroutinefunction(getattr(GeminiProvider, method))
