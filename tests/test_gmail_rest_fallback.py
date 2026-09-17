"""Gmail over REST, for the installs whose hosted MCP answer is PERMISSION_DENIED.

`tests/test_gmail_mcp_client.py` covers the common reads. This covers the rest
of the fallback: every failure status becomes a readable Gmail error, empty
results say so rather than returning nothing, a body is found in HTML when
there is no plain part and cut short when it is long, and a thread lists every
message in it.
"""

import base64
from typing import Any

import pytest

from wactorz.core.integrations import gmail_mcp
from wactorz.core.integrations.gmail_mcp import (
    GmailMcpClient,
    _decode_b64,
    _extract_body,
    _strip_html,
)
from wactorz.core.integrations.google_mcp import GoogleRestError


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class _Rest:
    """Answers `_rest_request` from a list of (status, body) pairs, in order."""

    def __init__(self, *answers: tuple[int, dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str, Any]] = []

    async def __call__(
        self, method: str, path: str, params: Any = None, json_body: Any = None
    ) -> tuple[int, dict[str, Any]]:
        self.requests.append((method, path, params if json_body is None else json_body))
        return self.answers.pop(0)


def _client(
    monkeypatch: pytest.MonkeyPatch, *answers: tuple[int, dict[str, Any]]
) -> tuple[GmailMcpClient, _Rest]:
    client = GmailMcpClient()
    rest = _Rest(*answers)
    monkeypatch.setattr(client, "_rest_request", rest)
    return client, rest


ERROR = (403, {"error": {"message": "Insufficient Permission"}})


class TestFailures:
    @pytest.mark.parametrize(
        ("handler", "arguments"),
        [
            ("_rest_read_email", {"id": "m1"}),
            ("_rest_read_email", {"query": "bank"}),
            ("_rest_get_thread", {"threadId": "t1"}),
            ("_rest_list_labels", {}),
            ("_rest_list_drafts", {}),
            ("_rest_create_draft", {"to": "a@b.c"}),
        ],
    )
    async def test_a_failed_request_is_a_readable_error(
        self, monkeypatch: pytest.MonkeyPatch, handler: str, arguments: dict[str, Any]
    ) -> None:
        client, _ = _client(monkeypatch, ERROR)

        with pytest.raises(GoogleRestError, match="Insufficient Permission"):
            await getattr(client, handler)(arguments)

    async def test_a_metadata_scope_refusing_search_says_what_to_do(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = _client(
            monkeypatch,
            (403, {"error": {"message": "Metadata scope does not support 'q' parameter"}}),
        )

        with pytest.raises(GoogleRestError, match="needs a non-metadata scope"):
            await client._rest_search({"query": "invoice"})

    async def test_a_thread_needs_an_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = _client(monkeypatch)

        with pytest.raises(GoogleRestError, match="Missing thread id"):
            await client._rest_get_thread({})


class TestEmptyResults:
    async def test_each_listing_says_when_there_is_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = _client(
            monkeypatch,
            (200, {}),
            (200, {}),
            (200, {}),
            (200, {}),
            (200, {"labels": [{"id": "x"}]}),
            (200, {}),
        )

        assert (
            await client._rest_search({"query": "is:unread"}) == "No emails found for 'is:unread'."
        )
        assert await client._rest_read_email({"q": "nothing"}) == "No email found for 'nothing'."
        assert await client._rest_get_thread({"id": "t1"}) == "Thread has no messages."
        assert await client._rest_list_drafts({}) == "No drafts."
        assert await client._rest_list_labels({}) == "No labels found."
        assert await client._rest_list_labels({}) == "No labels found."

    async def test_a_message_whose_metadata_fails_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = {
            "payload": {
                "headers": [
                    {"name": "From", "value": "Ann <a@x>"},
                    {"name": "Subject", "value": "Hi"},
                ]
            }
        }
        client, rest = _client(
            monkeypatch, (200, {"messages": [{"id": "1"}, {"id": "2"}]}), (200, good), ERROR
        )

        result = await client._rest_search({"q": "hello", "maxResults": 5})

        assert result == "• Ann — Hi"
        assert rest.requests[0][2] == {"q": "hello", "maxResults": "5"}


class TestReading:
    async def test_the_top_match_is_opened_and_a_long_body_is_cut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = {
            "payload": {
                "headers": [
                    {"name": "From", "value": "Bank"},
                    {"name": "Subject", "value": "Statement"},
                    {"name": "Date", "value": "Mon"},
                ],
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>" + "word " * 500 + "</p>")},
                    }
                ],
            }
        }
        client, rest = _client(monkeypatch, (200, {"messages": [{"id": "abc/1"}]}), (200, message))

        result = await client._rest_read_email({"query": "bank"})

        assert rest.requests[1][1] == "/users/me/messages/abc%2F1"
        assert result.startswith("From: Bank\nSubject: Statement\nDate: Mon\n\nword word")
        assert result.endswith("… (truncated)")

    async def test_an_email_with_no_text_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = _client(monkeypatch, (200, {"payload": {"headers": []}}))

        result = await client._rest_read_email({"messageId": "m1"})

        assert (
            result == "From: (unknown sender)\nSubject: (no subject)\n\n(no readable text content)"
        )

    async def test_a_thread_lists_every_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        msg = {"payload": {"headers": [{"name": "From", "value": "Bo"}]}, "snippet": "see you"}
        client, _ = _client(monkeypatch, (200, {"messages": [msg, msg]}))

        result = await client._rest_get_thread({"thread_id": "t1"})

        assert result.count("• Bo — (no subject)") == 2

    async def test_labels_and_drafts_are_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _ = _client(
            monkeypatch,
            (200, {"labels": [{"name": "INBOX"}, {"name": "Work"}]}),
            (200, {"drafts": [{}, {}]}),
        )

        assert await client._rest_list_labels({}) == "Your labels:\n• INBOX\n• Work"
        assert await client._rest_list_drafts({}) == "You have 2 draft(s)."

    async def test_a_draft_without_a_recipient_is_still_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, rest = _client(monkeypatch, (201, {"id": "d1"}))

        result = await client._rest_create_draft({"summary": "Notes", "text": "remember milk"})

        raw = base64.urlsafe_b64decode(rest.requests[0][2]["message"]["raw"]).decode()
        assert raw == "Subject: Notes\r\n\r\nremember milk"
        assert result == "Draft created: 'Notes'."


class TestBodyExtraction:
    def test_nested_plain_text_wins_over_html(self) -> None:
        payload = {
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<b>html</b>")}},
                {"parts": [{"mimeType": "text/plain", "body": {"data": _b64("plain text")}}]},
            ]
        }

        assert _extract_body(payload) == "plain text"

    def test_nothing_readable_is_empty(self) -> None:
        assert _extract_body({"mimeType": "text/plain", "body": {}}) == ""

    def test_html_is_reduced_to_text(self) -> None:
        text = _strip_html(
            "<style>x{}</style><p>One</p>Two<br/>Three &amp; four<script>bad()</script>"
        )

        assert "x{}" not in text and "bad()" not in text
        assert "One\n" in text and "Two\nThree & four" in text

    def test_undecodable_base64_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _broken(_data: Any) -> bytes:
            raise ValueError("bad padding")

        monkeypatch.setattr(gmail_mcp.base64, "urlsafe_b64decode", _broken)

        assert _decode_b64("abc") == ""

    def test_the_config_helpers_describe_gmail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for suffix in ("CLIENT_ID", "TOKEN", "AUTHORIZATION"):
            monkeypatch.delenv(f"GMAIL_MCP_{suffix}", raising=False)
        monkeypatch.setenv("GMAIL_MCP_URL", "https://gmail.example/mcp/")

        assert gmail_mcp.gmail_mcp_url() == "https://gmail.example/mcp"
        assert gmail_mcp.gmail_mcp_config_status()["gmail_mcp_auth"] is False
