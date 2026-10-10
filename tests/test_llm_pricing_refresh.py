"""Refreshing model prices from LiteLLM's catalogue.

The fetch happens at most once a day and never twice at once. Only entries that
carry both an input and an output price are kept, converted to dollars per
million tokens to match the built-in table; a failed fetch keeps whatever was
there, so costing carries on from the fallback prices.
"""

import time
from typing import Any

import pytest

from wactorz.agents.llm import pricing


class _Response:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.fetches = 0

    def __call__(self) -> "_Session":
        return self

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, timeout: Any = None) -> _Response:
        self.fetches += 1
        return _Response(self.data)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, "_dynamic_pricing", {})
    monkeypatch.setattr(pricing, "_dynamic_pricing_ts", 0.0)
    monkeypatch.setattr(pricing, "_pricing_fetch_in_progress", False)


def _serve(monkeypatch: pytest.MonkeyPatch, data: Any) -> _Session:
    session = _Session(data)
    monkeypatch.setattr(pricing.aiohttp, "ClientSession", session)
    return session


async def test_complete_entries_are_kept_per_million_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        {
            "sample_spec": "not a model",
            "model-a": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
            "model-b": {"input_cost_per_token": 0.000001},
        },
    )

    await pricing.refresh_pricing()

    assert pricing._dynamic_pricing == {"model-a": pytest.approx((1.0, 2.0))}
    assert pricing._pricing_fetch_in_progress is False


async def test_a_fresh_catalogue_is_not_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _serve(monkeypatch, {})
    monkeypatch.setattr(pricing, "_dynamic_pricing", {"model-a": (1.0, 2.0)})
    monkeypatch.setattr(pricing, "_dynamic_pricing_ts", time.time())

    await pricing.refresh_pricing()

    assert session.fetches == 0


async def test_a_fetch_already_running_is_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _serve(monkeypatch, {})
    monkeypatch.setattr(pricing, "_pricing_fetch_in_progress", True)

    await pricing.refresh_pricing()

    assert session.fetches == 0


async def test_a_failed_fetch_keeps_the_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _serve(monkeypatch, ValueError("not json"))

    await pricing.refresh_pricing()

    assert pricing._dynamic_pricing == {}
    assert "using fallback" in caplog.text
    assert pricing._pricing_fetch_in_progress is False
