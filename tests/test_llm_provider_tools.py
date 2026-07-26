"""Test LLM provider tools."""

# pylint: disable=missing-function-docstring,missing-class-docstring

from __future__ import annotations

import types
import unittest
from typing import Any
from unittest.mock import patch

import aiohttp

from tests.optional_deps import ensure_importable  # pyright: ignore[reportMissingImports]

ensure_importable("openai", "anthropic")

from wactorz.agents.llm_agent import (
    AnthropicProvider,
    GeminiProvider,
    NIMProvider,
    OllamaProvider,
    OpenAIProvider,
)

TOOL = {
    "name": "get_simplified_ha_data",
    "description": "Fetch HA data",
    "parameters": {"type": "object", "properties": {}},
}


class _FakeOpenAICompletions:  # pylint: disable=too-few-public-methods
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        message = types.SimpleNamespace(
            content=None,
            tool_calls=[
                types.SimpleNamespace(
                    id="call-1",
                    function=types.SimpleNamespace(name="get_simplified_ha_data", arguments="{}"),
                )
            ],
        )
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message)],
            usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


class _FakeAnthropicMessages:  # pylint: disable=too-few-public-methods
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: dict[str, Any]) -> types.SimpleNamespace:
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(
                    type="tool_use",
                    id="toolu-1",
                    name="get_simplified_ha_data",
                    input={},
                )
            ],
            usage=types.SimpleNamespace(input_tokens=5, output_tokens=1),
        )


class ProviderToolPlumbingTest(unittest.IsolatedAsyncioTestCase):
    async def test_openai_tool_payload_and_tool_result_message_shape(self) -> None:
        """OpenAI uses Chat Completions `tools` and `tool` response messages."""
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.model = "gpt-5-mini"
        completions = _FakeOpenAICompletions()
        provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))  # pyright: ignore[reportAttributeAccessIssue]

        result = await provider.complete_with_tools(
            messages=[
                {"role": "user", "content": "check HA"},
                {
                    "role": "tool",
                    "tool_call_id": "call-0",
                    "name": "get_simplified_ha_data",
                    "content": "{}",
                },
            ],
            tools=[TOOL],
            system="system",
        )

        payload = completions.calls[0]
        self.assertEqual(payload["tools"][0]["function"]["name"], "get_simplified_ha_data")
        self.assertEqual(payload["messages"][-1]["role"], "tool")
        self.assertEqual(payload["messages"][-1]["tool_call_id"], "call-0")
        self.assertEqual(result.tool_calls[0].name, "get_simplified_ha_data")

    async def test_nim_uses_openai_compatible_tool_payload(self) -> None:
        """NIM follows the same OpenAI-compatible tool payload shape."""
        provider = NIMProvider.__new__(NIMProvider)
        provider.model = "meta/llama-3.3-nemotron-super-49b-v1"
        completions = _FakeOpenAICompletions()
        provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))  # pyright: ignore[reportAttributeAccessIssue]

        result = await provider.complete_with_tools(
            messages=[{"role": "user", "content": "check HA"}],
            tools=[TOOL],
        )

        payload = completions.calls[0]
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["name"], "get_simplified_ha_data")
        self.assertEqual(result.tool_calls[0].id, "call-1")

    async def test_anthropic_tool_use_and_tool_result_block_shape(self) -> None:
        """Anthropic converts tool results into `tool_result` content blocks."""
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider.model = "claude-sonnet-4-6"
        messages_client = _FakeAnthropicMessages()
        provider.client = types.SimpleNamespace(messages=messages_client)  # pyright: ignore[reportAttributeAccessIssue]

        result = await provider.complete_with_tools(
            messages=[
                {"role": "user", "content": "check HA"},
                {"role": "tool", "tool_call_id": "toolu-0", "content": "{}"},
            ],
            tools=[TOOL],
        )

        payload = messages_client.calls[0]
        self.assertEqual(payload["tools"][0]["name"], "get_simplified_ha_data")
        self.assertEqual(payload["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertEqual(result.tool_calls[0].id, "toolu-1")

    async def test_ollama_tool_payload_and_returned_tool_calls(self) -> None:
        """Ollama receives OpenAI-style tools and returns normalized tool calls."""
        posted_payloads: list[dict[str, Any]] = []

        class _Response:
            async def __aenter__(self) -> _Response:
                return self

            async def __aexit__(
                self,
                exc_type: type[Exception] | None,
                exc: Exception | None,
                tb: types.TracebackType,
            ) -> None:
                return None

            async def json(self) -> dict[str, Any]:
                return {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "get_simplified_ha_data",
                                    "arguments": {},
                                },
                            }
                        ],
                    },
                    "prompt_eval_count": 2,
                    "eval_count": 1,
                }

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(
                self,
                exc_type: type[Exception] | None,
                exc: Exception | None,
                tb: types.TracebackType,
            ) -> None:
                return None

            def post(self, url: str, json: dict[str, Any]) -> _Response:
                posted_payloads.append(json)
                return _Response()

        # Patch only ClientSession on the real aiohttp — never swap the whole
        # module in sys.modules, which would change aiohttp's class identity
        # (e.g. web.AppKey) for every later test. start()+addCleanup rather than
        # self.enterContext (3.11+).
        _patch = patch.object(aiohttp, "ClientSession", lambda *a, **k: _Session())
        _patch.start()
        self.addCleanup(_patch.stop)

        provider = OllamaProvider(model="llama3", base_url="http://ollama.local")
        result = await provider.complete_with_tools(
            messages=[{"role": "user", "content": "check HA"}],
            tools=[TOOL],
        )

        self.assertEqual(
            posted_payloads[0]["tools"][0]["function"]["name"], "get_simplified_ha_data"
        )
        self.assertEqual(result.tool_calls[0].name, "get_simplified_ha_data")

    async def test_gemini_function_declaration_and_function_response_flow(self) -> None:
        """Gemini uses function declarations and function response parts."""

        class _Types:
            @staticmethod
            def GenerateContentConfig(**kwargs: Any) -> dict[str, Any]:
                return kwargs

            @staticmethod
            def Tool(**kwargs: Any) -> dict[str, Any]:
                return kwargs

            @staticmethod
            def FunctionDeclaration(**kwargs: Any) -> dict[str, Any]:
                return kwargs

        class _Models:
            """The async SDK surface — the provider must not block the loop."""

            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def generate_content(self, **kwargs: dict[str, Any]) -> types.SimpleNamespace:
                self.calls.append(kwargs)
                function_call = types.SimpleNamespace(
                    id="call-1",
                    name="get_simplified_ha_data",
                    args={},
                )
                part = types.SimpleNamespace(text=None, function_call=function_call)
                content = types.SimpleNamespace(parts=[part])
                return types.SimpleNamespace(
                    candidates=[types.SimpleNamespace(content=content)],
                    usage_metadata=types.SimpleNamespace(
                        prompt_token_count=2,
                        candidates_token_count=1,
                    ),
                )

        provider = GeminiProvider.__new__(GeminiProvider)
        provider.model_name = "gemini-2.5-flash"
        provider._types = _Types  # pyright: ignore[reportAttributeAccessIssue]
        models = _Models()
        provider.client = types.SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
            aio=types.SimpleNamespace(models=models)
        )

        result = await provider.complete_with_tools(
            messages=[{"role": "user", "content": "check HA"}],
            tools=[TOOL],
        )
        tool_contents = provider._to_gemini_contents(
            [
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "get_simplified_ha_data",
                    "content": "{}",
                }
            ]
        )

        config = models.calls[0]["config"]
        self.assertEqual(
            config["tools"][0]["function_declarations"][0]["name"], "get_simplified_ha_data"
        )
        self.assertEqual(result.tool_calls[0].name, "get_simplified_ha_data")
        self.assertEqual(
            tool_contents[0]["parts"][0]["function_response"]["name"],
            "get_simplified_ha_data",
        )


if __name__ == "__main__":
    unittest.main()
