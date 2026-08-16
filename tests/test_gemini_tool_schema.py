"""The tool schemas Gemini will actually accept.

`additionalProperties: false` is in the shared tool definitions because OpenAI's
strict tool calling wants it. Gemini's function-declaration format has no such
field and answers the whole request with a 400 — one violation per declaration
it appears in — so the keyword is dropped at this provider's boundary.

Nothing catches this before the request. The SDK's own `Schema` model declares
`additional_properties`, so the value validates and serializes without complaint
and only the API refuses; a filter written against the SDK's field list would
pass it straight through. These tests assert on what is serialized, which is the
only place the difference is visible without a network call.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from wactorz.agents.llm.providers.gemini import GeminiProvider, _gemini_schema

STRICT_TOOL: dict[str, Any] = {
    "name": "set_light",
    "description": "Turn a light on or off",
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string"},
            "options": {
                "type": "object",
                "properties": {"brightness": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        "required": ["entity_id"],
        "additionalProperties": False,
    },
}


def _declaration(tool: dict[str, Any]) -> dict[str, Any]:
    """What the provider hands the SDK, serialized as it would be sent.

    Built through `__new__` so no API key is needed: constructing a provider
    normally builds a real client, which refuses to exist without one.
    """
    provider = GeminiProvider.__new__(GeminiProvider)
    provider._types = types  # pyright: ignore[reportAttributeAccessIssue]
    declaration = provider._gemini_function_declaration(tool)
    return declaration.model_dump(exclude_none=True)


class TestWhatGeminiIsSent:
    def test_the_keyword_it_rejects_is_gone(self) -> None:
        dumped = _declaration(STRICT_TOOL)

        assert "additional_properties" not in str(dumped)
        assert "additionalProperties" not in str(dumped)

    def test_a_nested_property_is_cleaned_too(self) -> None:
        # The API reports one violation per declaration, so a nested keyword
        # fails the request exactly as a top-level one does.
        cleaned = _gemini_schema(STRICT_TOOL["parameters"])

        assert "additionalProperties" not in cleaned["properties"]["options"]

    def test_everything_else_survives(self) -> None:
        cleaned = _gemini_schema(STRICT_TOOL["parameters"])

        assert cleaned["required"] == ["entity_id"]
        assert cleaned["properties"]["entity_id"] == {"type": "string"}
        assert cleaned["properties"]["options"]["properties"] == {"brightness": {"type": "integer"}}

    def test_the_declaration_still_names_its_tool(self) -> None:
        dumped = _declaration(STRICT_TOOL)

        assert dumped["name"] == "set_light"
        assert dumped["description"] == "Turn a light on or off"
        assert dumped["parameters"]["required"] == ["entity_id"]

    def test_a_tool_with_no_parameters_is_unharmed(self) -> None:
        dumped = _declaration({"name": "list_cameras", "description": "x"})

        assert dumped["parameters"]["properties"] == {}


class TestTheStrippingItself:
    @pytest.mark.parametrize("key", ["additionalProperties", "additional_properties"])
    def test_both_spellings_go(self, key: str) -> None:
        # The shared definitions use the JSON Schema name; a schema that has
        # already been through the SDK carries the renamed one.
        assert not _gemini_schema({key: False})

    def test_a_schema_with_nothing_to_strip_is_unchanged(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}

        assert _gemini_schema(schema) == schema

    def test_it_reaches_inside_lists(self) -> None:
        # `anyOf` holds a list of schemas, each able to carry the keyword.
        schema = {"anyOf": [{"type": "object", "additionalProperties": False}, {"type": "null"}]}

        assert _gemini_schema(schema) == {"anyOf": [{"type": "object"}, {"type": "null"}]}


class TestTheSignatureOnAToolCall:
    """Gemini 3 returns an opaque signature on the part that made a call and
    requires it back on the turn reporting the result.

    The assistant turn is rebuilt from the extracted `ToolCall`, which carries
    the id, name and arguments and nothing else — so the signature was dropped
    and the next request refused with a 400.
    """

    @staticmethod
    def _provider(signature: bytes | None) -> Any:
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
            async def generate_content(self, **_kwargs: Any) -> SimpleNamespace:
                part = SimpleNamespace(
                    text=None,
                    thought_signature=signature,
                    function_call=SimpleNamespace(id="c1", name="get_ha_data", args={}),
                )
                return SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
                    usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
                )

        provider = GeminiProvider.__new__(GeminiProvider)
        provider.model_name = "gemini-3.6-flash"
        provider._types = _Types  # pyright: ignore[reportAttributeAccessIssue]
        provider.client = SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
            aio=SimpleNamespace(models=_Models())
        )
        return provider

    async def _assistant_parts(self, signature: bytes | None) -> list[dict[str, Any]]:
        completion = await self._provider(signature).complete_with_tools(
            messages=[{"role": "user", "content": "which lights are on?"}],
            tools=[{"name": "get_ha_data", "description": "x", "parameters": {"type": "object"}}],
        )
        assert completion.assistant_message is not None
        return completion.assistant_message["content"]

    async def test_it_rides_back_on_the_call_that_earned_it(self) -> None:
        parts = await self._assistant_parts(b"sig-bytes")

        assert parts[0]["thought_signature"] == b"sig-bytes"
        assert parts[0]["function_call"]["name"] == "get_ha_data"

    async def test_a_model_that_sends_none_adds_no_field(self) -> None:
        # Only the 3.x line signs its calls; sending an empty field to a model
        # that never asked for one is a different way to be wrong.
        parts = await self._assistant_parts(None)

        assert "thought_signature" not in parts[0]

    async def test_the_turn_survives_the_translation_it_is_fed_back_through(self) -> None:
        # The assistant turn goes back through `_to_gemini_contents` on the next
        # iteration, which must leave both the call and its signature intact.
        parts = await self._assistant_parts(b"sig-bytes")
        provider = self._provider(b"sig-bytes")

        contents = provider._to_gemini_contents([{"role": "assistant", "content": parts}])

        assert contents[0]["parts"] == parts
