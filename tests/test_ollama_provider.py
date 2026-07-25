import json
import sys
import types
import unittest
from unittest.mock import patch

import aiohttp

sys.modules.setdefault("openai", types.ModuleType("openai"))

from wactorz.agents.llm_agent import OllamaProvider


class _FakeResponse:
    def __init__(self, json_data=None, content_chunks=None):
        self._json_data = json_data or {}
        self.content = _FakeContent(content_chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self._json_data


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeSession:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, json):
        self._calls.append({"url": url, "json": json})
        return self._response


class OllamaProviderTest(unittest.IsolatedAsyncioTestCase):
    # The provider does a function-local ``import aiohttp; aiohttp.ClientSession()``.
    # Patch only that attribute on the real module — never swap the whole module in
    # sys.modules, which changes aiohttp's class identity for every later test.

    async def test_complete_sends_system_prompt_as_system_message(self):
        calls = []
        session = _FakeSession(
            _FakeResponse(
                json_data={
                    "message": {"content": "hello"},
                    "prompt_eval_count": 3,
                    "eval_count": 2,
                }
            ),
            calls,
        )

        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: session):
            text, usage = await provider.complete(
                messages=[{"role": "user", "content": "ping"}],
                system="You are concise.",
            )

        self.assertEqual(text, "hello")
        self.assertEqual(usage["input_tokens"], 3)
        self.assertEqual(usage["output_tokens"], 2)
        self.assertEqual(
            calls[0]["json"]["messages"],
            [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": "ping"},
            ],
        )
        self.assertNotIn("system", calls[0]["json"])

    async def test_stream_sends_system_prompt_as_system_message(self):
        calls = []
        chunks = [
            json.dumps({"message": {"content": "he"}, "done": False}).encode(),
            json.dumps(
                {
                    "message": {"content": "llo"},
                    "done": True,
                    "prompt_eval_count": 5,
                    "eval_count": 7,
                }
            ).encode(),
        ]
        session = _FakeSession(_FakeResponse(content_chunks=chunks), calls)

        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        parts = []
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: session):
            async for chunk in provider.stream(
                messages=[{"role": "user", "content": "ping"}],
                system="You are concise.",
            ):
                parts.append(chunk)

        self.assertEqual(parts[:-1], ["he", "llo"])
        self.assertEqual(parts[-1]["input_tokens"], 5)
        self.assertEqual(parts[-1]["output_tokens"], 7)
        self.assertEqual(
            calls[0]["json"]["messages"],
            [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": "ping"},
            ],
        )
        self.assertNotIn("system", calls[0]["json"])
