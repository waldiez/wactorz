import unittest
from unittest import mock

from wactorz.core.integrations.gmail_mcp import GmailMcpClient, _format_message_line


def _fake_rest(mapping):
    async def _rest(method, path, params=None, json_body=None):
        if path == "/users/me/messages":
            return (200, {"messages": [{"id": "m1"}, {"id": "m2"}]})
        if path.startswith("/users/me/messages/"):
            mid = path.rsplit("/", 1)[1]
            return (
                200,
                {
                    "id": mid,
                    "snippet": f"snippet {mid}",
                    "payload": {"headers": [
                        {"name": "From", "value": "Bob <bob@x.com>"},
                        {"name": "Subject", "value": f"Subj {mid}"},
                    ]},
                },
            )
        return mapping.get(path, (200, {}))

    return _rest


class FormatMessageTest(unittest.TestCase):
    def test_trims_sender_and_subject(self):
        line = _format_message_line({
            "snippet": "hi there",
            "payload": {"headers": [
                {"name": "From", "value": "Alice Example <alice@x.com>"},
                {"name": "Subject", "value": "Lunch?"},
            ]},
        })
        self.assertIn("Alice Example", line)
        self.assertIn("Lunch?", line)
        self.assertIn("hi there", line)


class GmailRestFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_falls_back_and_formats(self):
        client = GmailMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(side_effect=_fake_rest({}))

        result = await client.call_tool("search_threads", {"query": "is:unread", "pageSize": 10})

        self.assertIn("Bob", result)
        self.assertIn("Subj m1", result)
        # 'is:unread' maps to a labelIds filter (metadata scope forbids 'q')
        first = client._rest_request.await_args_list[0]
        self.assertEqual(first.args[1], "/users/me/messages")
        self.assertEqual(first.kwargs["params"]["labelIds"], "UNREAD")
        self.assertNotIn("q", first.kwargs["params"])

    async def test_free_text_search_uses_q(self):
        client = GmailMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(side_effect=_fake_rest({}))

        await client.call_tool("search_threads", {"query": "from:bob@x.com", "pageSize": 10})

        params = client._rest_request.await_args_list[0].kwargs["params"]
        self.assertEqual(params["q"], "from:bob@x.com")

    async def test_create_draft_builds_raw_message(self):
        client = GmailMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="PERMISSION_DENIED")
        client._rest_request = mock.AsyncMock(return_value=(200, {"id": "d1"}))

        result = await client.call_tool(
            "create_draft", {"to": "sam@x.com", "subject": "Hi", "body": "running late"}
        )

        self.assertIn("Draft to sam@x.com created", result)
        method, path = client._rest_request.await_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/users/me/drafts")
        raw = client._rest_request.await_args.kwargs["json_body"]["message"]["raw"]
        import base64
        decoded = base64.urlsafe_b64decode(raw).decode()
        self.assertIn("To: sam@x.com", decoded)
        self.assertIn("Subject: Hi", decoded)
        self.assertIn("running late", decoded)

    async def test_labels_fallback(self):
        client = GmailMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(
            return_value=(200, {"labels": [{"name": "INBOX"}, {"name": "Work"}]})
        )

        result = await client.call_tool("list_labels", {})

        self.assertIn("INBOX", result)
        self.assertIn("Work", result)

    def test_store_token_response_preserves_refresh(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tok.json")
            with mock.patch.dict(os.environ, {"GMAIL_MCP_TOKEN_FILE": path}):
                client = GmailMcpClient()
                client._store_token_response({"access_token": "a1", "refresh_token": "r1", "scope": "s"})
                # A refresh response without a refresh_token must not wipe the stored one.
                client._store_token_response({"access_token": "a2"})
                tokens = json.loads(open(path).read())["tokens"]
        self.assertEqual(tokens["access_token"], "a2")
        self.assertEqual(tokens["refresh_token"], "r1")

    async def test_no_fallback_when_mcp_ok(self):
        client = GmailMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="thread details here")
        client._rest_request = mock.AsyncMock()

        result = await client.call_tool("get_thread", {"threadId": "t1"})

        self.assertEqual(result, "thread details here")
        client._rest_request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
