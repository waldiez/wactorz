"""The Gmail agent's request flow: understanding a request, then serving it.

A request is understood by the model when there is one and by a rule-based
parser when there is not, or when the model answers with something that is not
an action. Reading one email goes further: the model answers the question asked
about it, and when it cannot the email is returned as it is.

Drafting is never sending. A draft missing its recipient or body is held, and
the next reply fills in whichever part was asked for.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import gmail_agent as gmail_mod
from wactorz.agents.gmail_agent import GmailAgent, _extract_json, _fallback_parse
from wactorz.core.actor import Message, MessageType


class _Llm:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, {"input_tokens": 3, "output_tokens": 1, "cost_usd": 0.001}


class _Client:
    def __init__(self, answer: str | Exception = "ok") -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        self.calls.append((tool, args))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _agent(
    tmp_path: Path, llm: _Llm | None = None, answer: str | Exception = "ok"
) -> tuple[GmailAgent, _Client]:
    agent = GmailAgent(llm_provider=None, persistence_dir=str(tmp_path))
    agent.llm = llm  # pyright: ignore[reportAttributeAccessIssue]
    client = _Client(answer)
    agent.client = client  # pyright: ignore[reportAttributeAccessIssue]
    return agent, client


class TestUnderstanding:
    async def test_a_structured_operation_is_used_as_given(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        await agent._process({"operation": "search", "query": "from:bank", "count": 3})

        assert client.calls == [("search_threads", {"query": "from:bank", "pageSize": 3})]

    async def test_an_empty_request_shows_the_inbox(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        await agent._process({})

        assert client.calls == [("search_threads", {"query": "in:inbox", "pageSize": 10})]

    async def test_the_model_decides_the_action(self, tmp_path: Path) -> None:
        llm = _Llm('```json\n{"action": "drafts"}\n```')
        agent, client = _agent(tmp_path, llm)

        await agent._process({"text": "what have I not sent yet"})

        assert client.calls == [("list_drafts", {})]
        assert agent.total_cost_usd > 0

    @pytest.mark.parametrize("reply", ['{"note": "no action"}', "no json", RuntimeError("down")])
    async def test_an_unusable_model_answer_falls_back_to_the_parser(
        self, tmp_path: Path, reply: str | Exception
    ) -> None:
        agent, client = _agent(tmp_path, _Llm(reply))

        await agent._process({"text": "show my labels"})

        assert client.calls == [("list_labels", {})]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("help", {"action": "help"}),
            (
                "compose an email to a@b.com saying hi",
                {"action": "create_draft", "to": "a@b.com", "body": "hi"},
            ),
            ("what does the vodafone bill say", {"action": "read", "query": "vodafone bill"}),
            ("any new mail", {"action": "unread", "count": 10}),
            ("show folders", {"action": "labels"}),
            (
                "messages from Alice Smith",
                {"action": "search", "query": "from:Alice Smith", "count": 10},
            ),
            ("find invoices?", {"action": "search", "query": "invoices", "count": 10}),
            ("recent stuff", {"action": "inbox", "count": 10}),
            ("hmm", {"action": "inbox", "count": 10}),
        ],
    )
    def test_the_rule_based_parser(self, text: str, expected: dict[str, Any]) -> None:
        assert _fallback_parse(text) == expected

    def test_json_is_found_inside_prose_or_fences(self) -> None:
        assert _extract_json('Sure: {"a": 1} done') == '{"a": 1}'
        assert _extract_json("```\nnothing\n```") == "nothing"


class TestServing:
    async def test_status_reports_whether_gmail_is_connected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gmail_mod, "gmail_mcp_config_status", lambda: {"gmail_mcp_auth": True})
        agent, _ = _agent(tmp_path)

        result = await agent._process({"operation": "status"})

        assert result["result"].startswith("Gmail is connected.")

    async def test_reading_answers_the_question_about_the_email(self, tmp_path: Path) -> None:
        llm = _Llm("You owe 42.10 EUR by 5 May.")
        agent, client = _agent(tmp_path, llm, answer="From: Vodafone\nAmount due: 42.10 EUR")

        result = await agent._process(
            {"operation": "read", "query": "vodafone", "text": "how much do I owe?"}
        )

        assert client.calls == [("read_email", {"query": "vodafone"})]
        assert result == {
            "result": "You owe 42.10 EUR by 5 May.",
            "email": "From: Vodafone\nAmount due: 42.10 EUR",
        }
        assert "QUESTION: how much do I owe?" in llm.calls[0]["messages"][0]["content"]

    async def test_reading_by_id_without_a_model_returns_the_email(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path, answer="the email")

        result = await agent._process({"operation": "read", "threadId": "abc"})

        assert client.calls == [("read_email", {"id": "abc"})]
        assert result == {"result": "the email"}

    @pytest.mark.parametrize(
        ("answer", "llm_reply"), [("No email matched", "x"), ("body", RuntimeError("down"))]
    )
    async def test_an_unanswerable_read_returns_the_email_as_it_is(
        self, tmp_path: Path, answer: str, llm_reply: str | Exception
    ) -> None:
        agent, _ = _agent(tmp_path, _Llm(llm_reply), answer=answer)

        assert await agent._process({"operation": "read"}) == {"result": answer}

    async def test_summarising_without_a_model_says_so(self, tmp_path: Path) -> None:
        agent, _ = _agent(tmp_path)

        assert "No LLM provider" in await agent._answer_from_email("x", "")

    async def test_a_draft_is_created_when_complete_and_held_when_not(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        held = await agent._process({"operation": "create_draft", "to": "sam@x.com"})
        done = await agent._process({"text": "running 10 minutes late"})

        assert held == {"result": "What should the email say?", "missing": ["body"]}
        assert client.calls == [
            (
                "create_draft",
                {"to": "sam@x.com", "subject": "(no subject)", "body": "running 10 minutes late"},
            )
        ]
        assert done == {"result": "ok"}
        assert agent._awaiting_draft is False

    async def test_a_held_draft_takes_a_bare_address_as_its_recipient(self, tmp_path: Path) -> None:
        agent, _ = _agent(tmp_path)
        await agent._process({"operation": "create_draft", "body": "hello"})

        resolved = await agent._resolve_action({"text": "it goes to sam@x.com please"})

        assert resolved == {"body": "hello", "action": "create_draft", "to": "sam@x.com"}

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({"operation": "create_draft", "body": "hi"}, "Who should I address the draft to?"),
            ({"operation": "create_draft"}, "Who is it to, and what should it say?"),
        ],
    )
    async def test_the_missing_part_is_asked_for(
        self, tmp_path: Path, payload: dict[str, Any], message: str
    ) -> None:
        agent, _ = _agent(tmp_path)

        assert (await agent._process(payload))["result"].startswith(message)

    async def test_unknown_actions_and_failures_are_explained(self, tmp_path: Path) -> None:
        agent, _ = _agent(tmp_path, answer=RuntimeError("token expired"))

        assert await agent._process({"operation": "send"}) == {
            "result": "Unsupported Gmail action: send"
        }
        failed = await agent._process({"operation": "labels"})

        assert failed == {"result": "Gmail error: token expired", "error": "token expired"}
        assert agent.metrics.tasks_failed == 1


class TestEntryPoints:
    async def test_chat_records_the_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _ = _agent(tmp_path, answer="3 unread")
        monkeypatch.setattr(agent, "_log_chat_turn", lambda *a, **k: None)

        chunks = [chunk async for chunk in agent.chat_stream("any unread email?")]

        assert chunks == ["3 unread", {}]
        assert [m["role"] for m in agent._conversation_history[-2:]] == ["user", "assistant"]

    async def test_a_task_is_answered_to_the_requester(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _ = _agent(tmp_path, answer="labels")
        sent: list[tuple[str, Any]] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, payload))
            return True

        monkeypatch.setattr(agent, "send", _send)

        await agent.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="main",
                payload={"operation": "labels", "_task_id": "t1", "_reply_to": "planner"},
            )
        )
        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="", payload="list labels")
        )
        await agent.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="main"))

        assert sent == [("planner", {"result": "labels", "task": "labels", "_task_id": "t1"})]
        assert agent.metrics.tasks_completed == 2
