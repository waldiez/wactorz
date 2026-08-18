"""Every provider translates attachment blocks into its own wire shape.

`supports_blocks()` tells the agent a list-of-blocks content is safe to send,
so the translation each provider applies is the contract that makes it true.
These pin the shapes, so a provider edit that drops or mangles an image fails
loudly rather than as a model that quietly cannot see.
"""

from wactorz.agents.llm.attachments import NO_DOCUMENTS, UNREADABLE
from wactorz.agents.llm.openai_shape import openai_messages
from wactorz.agents.llm.providers.anthropic import AnthropicProvider
from wactorz.agents.llm.providers.gemini import GeminiProvider
from wactorz.agents.llm.providers.nim import NIMProvider
from wactorz.agents.llm.providers.ollama import OllamaProvider
from wactorz.agents.llm.providers.openai import OpenAIProvider

TEXT = {"type": "text", "text": "[attachment: shot.png]"}
QUESTION = {"type": "text", "text": "what is this?"}
IMAGE = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
}
DOCUMENT = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": "cGRm"},
}
BLOCKS = [TEXT, IMAGE, QUESTION]


class TestAllShippedProvidersOptIn:
    def test_all_five_take_blocks(self) -> None:
        for cls in (
            AnthropicProvider,
            OpenAIProvider,
            NIMProvider,
            GeminiProvider,
            OllamaProvider,
        ):
            assert cls.supports_blocks() is True, cls.__name__


class TestOpenAIShape:
    """Shared by OpenAIProvider and NIMProvider via openai_shape."""

    def test_an_image_becomes_a_data_url_part(self) -> None:
        (msg,) = openai_messages([{"role": "user", "content": BLOCKS}])
        parts = msg["content"]
        assert parts[0] == {"type": "text", "text": "[attachment: shot.png]"}
        assert parts[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        }
        assert parts[2] == {"type": "text", "text": "what is this?"}

    def test_a_document_becomes_a_named_omission(self) -> None:
        (msg,) = openai_messages([{"role": "user", "content": [TEXT, DOCUMENT]}])
        # The name note survives; the PDF bytes must not — this format has no
        # inline document part, and a missing marker invites the model to
        # invent the contents.
        assert parts_text(msg["content"]) == ["[attachment: shot.png]", NO_DOCUMENTS]

    def test_plain_string_content_passes_through(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        assert openai_messages(msgs) == msgs


def parts_text(parts: list[dict]) -> list[str]:
    return [p["text"] for p in parts if p.get("type") == "text"]


class TestGeminiShape:
    def test_an_image_becomes_inline_data(self) -> None:
        contents = GeminiProvider._to_gemini_contents([{"role": "user", "content": BLOCKS}])
        (entry,) = contents
        assert entry["role"] == "user"
        assert entry["parts"][0] == {"text": "[attachment: shot.png]"}
        assert entry["parts"][1] == {"inline_data": {"mime_type": "image/png", "data": "aGVsbG8="}}
        assert entry["parts"][2] == {"text": "what is this?"}

    def test_a_document_is_kept_as_inline_data(self) -> None:
        # Gemini reads PDFs inline, so the document block must not degrade to
        # a marker here the way it does on the OpenAI shape.
        contents = GeminiProvider._to_gemini_contents(
            [{"role": "user", "content": [TEXT, DOCUMENT]}]
        )
        assert contents[0]["parts"][1] == {
            "inline_data": {"mime_type": "application/pdf", "data": "cGRm"}
        }

    def test_same_role_merging_still_applies(self) -> None:
        contents = GeminiProvider._to_gemini_contents(
            [
                {"role": "user", "content": BLOCKS},
                {"role": "user", "content": "and this?"},
            ]
        )
        (entry,) = contents
        assert len(entry["parts"]) == 4  # three block parts + the follow-up text


class TestOllamaShape:
    def test_images_move_to_the_message_level_field(self) -> None:
        (msg,) = OllamaProvider._chat_messages([{"role": "user", "content": BLOCKS}])
        assert msg["images"] == ["aGVsbG8="]
        assert msg["content"] == "[attachment: shot.png]\nwhat is this?"

    def test_a_document_becomes_a_named_omission(self) -> None:
        (msg,) = OllamaProvider._chat_messages([{"role": "user", "content": [TEXT, DOCUMENT]}])
        assert "images" not in msg
        assert msg["content"] == f"[attachment: shot.png]\n{NO_DOCUMENTS}"

    def test_plain_string_content_passes_through(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        assert OllamaProvider._chat_messages(msgs) == msgs


class TestNothingIsDroppedInSilence:
    """A block that cannot be carried is named, never quietly removed.

    `to_blocks` only builds a block once the bytes are in hand, so these are
    the translators' floor rather than a shape the send path produces.
    """

    def test_openai_names_an_image_with_no_data(self) -> None:
        blank = {"type": "image", "source": {"type": "base64", "media_type": "image/png"}}
        (msg,) = openai_messages([{"role": "user", "content": [blank]}])

        assert parts_text(msg["content"]) == [UNREADABLE]

    def test_gemini_names_an_image_with_no_data(self) -> None:
        blank = {"type": "image", "source": {"type": "base64", "media_type": "image/png"}}
        contents = GeminiProvider._to_gemini_contents([{"role": "user", "content": [blank]}])

        assert contents[0]["parts"] == [{"text": UNREADABLE}]

    def test_ollama_names_an_image_with_no_data(self) -> None:
        blank = {"type": "image", "source": {"type": "base64", "media_type": "image/png"}}
        (msg,) = OllamaProvider._chat_messages([{"role": "user", "content": [blank]}])

        assert "images" not in msg
        assert msg["content"] == UNREADABLE


class TestAListThatIsNotAttachments:
    """Translating a list that was never attachments is how a tool loop breaks.

    Gemini's own `_complete_with_tools` feeds the assistant turn back as Gemini
    parts, and the HA agent appends it to the messages for the next iteration.
    Dropping what is unrecognised took the `function_call` with it while the
    `function_response` answering it stayed — a request Gemini cannot resolve.
    """

    def test_gemini_keeps_an_assistant_turns_tool_call(self) -> None:
        assistant_parts = [
            {"text": "let me look that up"},
            {"function_call": {"id": "c1", "name": "get_simplified_ha_data", "args": {}}},
        ]

        contents = GeminiProvider._to_gemini_contents(
            [
                {"role": "user", "content": "which lights are on?"},
                {"role": "assistant", "content": assistant_parts},
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "name": "get_simplified_ha_data",
                    "content": "{}",
                },
            ]
        )

        assert contents[1] == {"role": "model", "parts": assistant_parts}
        # The call must still precede the response that answers it.
        assert "function_response" in contents[2]["parts"][0]

    def test_openai_keeps_a_part_it_did_not_produce(self) -> None:
        native = {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}}
        (msg,) = openai_messages([{"role": "user", "content": [native]}])

        assert msg["content"] == [native]
