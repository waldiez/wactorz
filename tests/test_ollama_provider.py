import json as json_
import unittest
from unittest.mock import patch

import aiohttp

from tests.optional_deps import ensure_importable  # pyright: ignore[reportMissingImports]

ensure_importable("openai")

from wactorz.agents.llm_agent import OllamaProvider


class _FakeResponse:
    def __init__(self, json_data=None, content_chunks=None, status=200):
        self._json_data = json_data or {}
        self.content = _FakeContent(content_chunks or [])
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message="boom"
            )

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
        self._iter = iter(self._chunks)  # pylint: disable=attribute-defined-outside-init
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

    def post(self, url, json, timeout=None):
        self._calls.append({"url": url, "json": json, "timeout": timeout})
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
            json_.dumps({"message": {"content": "he"}, "done": False}).encode(),
            json_.dumps(
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


class OllamaTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """A wedged Ollama must not hang the turn forever.

    Ollama is often reached over a LAN and can legitimately think for minutes, so
    the bound differs by call shape: a whole-request ceiling for the one-shot
    calls, and for streaming a gap-between-chunks bound instead — a long answer
    is not a fault, but silence is.
    """

    def _session(self, calls, **resp_kw):
        return _FakeSession(
            _FakeResponse(json_data={"message": {"content": "hi"}}, **resp_kw), calls
        )

    async def test_complete_bounds_the_request(self):
        calls = []
        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: self._session(calls)):
            await provider.complete(messages=[{"role": "user", "content": "ping"}])

        timeout = calls[0]["timeout"]
        self.assertIsNotNone(timeout, "no timeout passed — a wedged server hangs the turn")
        self.assertIsNotNone(timeout.total)
        self.assertIsNotNone(timeout.sock_connect)

    async def test_complete_with_tools_bounds_the_request(self):
        calls = []
        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: self._session(calls)):
            await provider.complete_with_tools(
                messages=[{"role": "user", "content": "ping"}], tools=[]
            )

        self.assertIsNotNone(calls[0]["timeout"])
        self.assertIsNotNone(calls[0]["timeout"].total)

    async def test_stream_bounds_the_gap_between_chunks_not_the_whole_stream(self):
        calls = []
        session = _FakeSession(_FakeResponse(content_chunks=[b'{"done":true}']), calls)
        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: session):
            async for _ in provider.stream(messages=[{"role": "user", "content": "ping"}]):
                pass

        timeout = calls[0]["timeout"]
        self.assertIsNotNone(timeout)
        self.assertIsNotNone(timeout.sock_read, "a stalled stream would never be noticed")
        self.assertIsNone(timeout.total, "a total would cut off a long but healthy stream")


class OllamaErrorTest(unittest.IsolatedAsyncioTestCase):
    """HTTP failures must surface, not be read as an empty reply."""

    async def test_complete_raises_on_http_error(self):
        calls = []
        session = _FakeSession(_FakeResponse(json_data={}, status=500), calls)
        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: session):
            with self.assertRaises(aiohttp.ClientResponseError):
                await provider.complete(messages=[{"role": "user", "content": "ping"}])

    async def test_complete_with_tools_raises_on_http_error(self):
        calls = []
        session = _FakeSession(_FakeResponse(json_data={}, status=503), calls)
        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        with patch.object(aiohttp, "ClientSession", lambda *a, **k: session):
            with self.assertRaises(aiohttp.ClientResponseError):
                await provider.complete_with_tools(
                    messages=[{"role": "user", "content": "ping"}], tools=[]
                )
