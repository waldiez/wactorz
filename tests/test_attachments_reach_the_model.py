"""Getting a turn's attachments from the chat path into the model request.

Two things are being pinned here. The first is that the blocks arrive at all —
the request the provider is handed carries them, in front of the user's text.
The second is what happens to a provider that cannot take them: it gets the
flattened reading rather than a list it would render with `str()`, which would
put a base64 payload into the prompt instead of dropping it.

History is the third: the blocks are deliberately not stored. A base64 image in
`_conversation_history` is written to disk every turn and re-sent on every turn
after it, so history keeps a text stand-in and the model sees the real thing
only on the turn it arrived.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm import attachments
from wactorz.agents.llm.base import LLMProvider
from wactorz.agents.llm_agent import LLMAgent

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class RecordingProvider(LLMProvider):
    """Captures the messages it was asked to send. Takes block content."""

    def __init__(self, blocks: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self._blocks = blocks

    def supports_blocks(self) -> bool:  # type: ignore[override]
        return self._blocks

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        self.sent = messages
        return "ok", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

    @property
    def last_user_content(self) -> Any:
        return self.sent[-1]["content"]


@pytest.fixture(name="agent")
def agent_fixture(tmp_path: Path) -> LLMAgent:
    return LLMAgent(
        llm_provider=RecordingProvider(),
        name="tester",
        persistence_dir=str(tmp_path),
    )


def _blocks(name: str = "shot.png", data: bytes = PNG, mime: str = "image/png") -> list[dict]:
    ref = {"id": "a" * 32, "name": name, "mime": mime, "size": len(data)}
    return attachments.to_blocks([ref], lambda _file_id: data)


class TestReachingTheModel:
    async def test_the_blocks_arrive_with_the_turn(self, agent: LLMAgent) -> None:
        await agent.chat("what is this?", _blocks())

        content = agent.llm.last_user_content  # pyright: ignore[reportAttributeAccessIssue]
        assert [b["type"] for b in content] == ["text", "image", "text"]

    async def test_the_question_comes_after_the_files(self, agent: LLMAgent) -> None:
        # The turn should read as being about the files, not the other way round.
        await agent.chat("what is this?", _blocks())

        content = agent.llm.last_user_content  # pyright: ignore[reportAttributeAccessIssue]
        assert content[-1] == {"type": "text", "text": "what is this?"}

    async def test_a_turn_without_attachments_is_unchanged(self, agent: LLMAgent) -> None:
        await agent.chat("plain question")

        assert agent.llm.last_user_content == "plain question"  # pyright: ignore[reportAttributeAccessIssue]


class TestAProviderThatCannotTakeBlocks:
    @pytest.fixture(name="text_only")
    def text_only_fixture(self, tmp_path: Path) -> LLMAgent:
        return LLMAgent(
            llm_provider=RecordingProvider(blocks=False),
            name="tester",
            persistence_dir=str(tmp_path),
        )

    async def test_it_is_sent_text_not_a_list(self, text_only: LLMAgent) -> None:
        await text_only.chat("what is this?", _blocks())

        assert isinstance(text_only.llm.last_user_content, str)  # pyright: ignore[reportAttributeAccessIssue]

    async def test_the_file_is_named_rather_than_dropped(self, text_only: LLMAgent) -> None:
        await text_only.chat("what is this?", _blocks(name="holiday.png"))

        content = text_only.llm.last_user_content  # pyright: ignore[reportAttributeAccessIssue]
        assert "holiday.png" in content
        assert "what is this?" in content

    async def test_no_encoded_payload_reaches_the_prompt(self, text_only: LLMAgent) -> None:
        # This is the failure the flag exists to prevent: a provider that
        # flattens with `str()` would inline the whole base64 string.
        await text_only.chat("what is this?", _blocks(data=PNG * 200))

        assert "iVBOR" not in text_only.llm.last_user_content  # pyright: ignore[reportAttributeAccessIssue]

    async def test_a_text_file_is_still_readable(self, text_only: LLMAgent) -> None:
        blocks = _blocks(name="sales.csv", data=b"a,b\n1,2\n", mime="text/csv")

        await text_only.chat("summarise", blocks)

        assert "a,b" in text_only.llm.last_user_content  # pyright: ignore[reportAttributeAccessIssue]


class TestWhatHistoryKeeps:
    async def test_the_encoded_file_is_never_stored(self, agent: LLMAgent) -> None:
        await agent.chat("what is this?", _blocks(data=PNG * 200))

        stored = str(agent._conversation_history)
        assert "iVBOR" not in stored

    async def test_the_turn_keeps_what_the_user_typed(self, agent: LLMAgent) -> None:
        # Callers match on this content — main swaps its live-context prefix back
        # out by comparing against it — so the attachments ride alongside.
        await agent.chat("what is this?", _blocks())

        assert agent._conversation_history[0]["content"] == "what is this?"

    async def test_a_later_turn_still_knows_a_file_was_attached(self, agent: LLMAgent) -> None:
        await agent.chat("what is this?", _blocks(name="receipt.png"))

        await agent.chat("what was it called again?")

        first_user = agent.llm.sent[0]  # pyright: ignore[reportAttributeAccessIssue]
        assert "receipt.png" in first_user["content"]

    async def test_the_later_turn_carries_no_blocks(self, agent: LLMAgent) -> None:
        await agent.chat("what is this?", _blocks())

        await agent.chat("and now?")

        assert all(isinstance(m["content"], str) for m in agent.llm.sent)  # pyright: ignore[reportAttributeAccessIssue]
