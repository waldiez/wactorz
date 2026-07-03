import unittest
from unittest import mock

from wactorz.agents.gmail_agent import GmailAgent


class GmailAgentTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self):
        return GmailAgent(llm_provider=None, persistence_dir="state/test-gmail")

    async def test_unread_searches_is_unread(self):
        agent = self._agent()
        agent.client.call_tool = mock.AsyncMock(return_value="• Bob — Hi")

        result = await agent._process({"text": "any unread email?"})

        self.assertIn("Bob", result["result"])
        tool, args = agent.client.call_tool.await_args.args
        self.assertEqual(tool, "search_threads")
        self.assertEqual(args["query"], "is:unread")

    async def test_from_sender_builds_query(self):
        agent = self._agent()
        agent.client.call_tool = mock.AsyncMock(return_value="results")

        await agent._process({"text": "emails from stripe@stripe.com"})

        tool, args = agent.client.call_tool.await_args.args
        self.assertEqual(tool, "search_threads")
        self.assertIn("from:stripe@stripe.com", args["query"])

    async def test_labels(self):
        agent = self._agent()
        agent.client.call_tool = mock.AsyncMock(return_value="Your labels:\n• INBOX")

        result = await agent._process({"text": "list my labels"})

        self.assertIn("INBOX", result["result"])
        tool, _ = agent.client.call_tool.await_args.args
        self.assertEqual(tool, "list_labels")

    async def test_help_is_user_friendly(self):
        agent = self._agent()
        result = await agent._process({"text": "what can you do"})
        self.assertIn("Gmail", result["result"])
        self.assertIn("draft", result["result"].lower())
        self.assertNotIn("gmail_mcp_url", result["result"])

    async def test_draft_followup_reuses_pending(self):
        agent = self._agent()
        agent.client.call_tool = mock.AsyncMock(return_value="Draft to sam@x.com created: 'Hi'.")

        first = await agent._process({"text": "draft an email"})
        self.assertIn("to", first["missing"])

        second = await agent._process({"text": "to sam@x.com saying running 10 min late"})

        self.assertIn("created", second["result"])
        tool, args = agent.client.call_tool.await_args.args
        self.assertEqual(tool, "create_draft")
        self.assertEqual(args["to"], "sam@x.com")
        self.assertIn("running 10 min late", args["body"])

    async def test_structured_search_operation(self):
        agent = self._agent()
        agent.client.call_tool = mock.AsyncMock(return_value="found")

        await agent._process({"operation": "search", "query": "subject:invoice", "count": 5})

        tool, args = agent.client.call_tool.await_args.args
        self.assertEqual(tool, "search_threads")
        self.assertEqual(args["query"], "subject:invoice")
        self.assertEqual(args["pageSize"], 5)


class GmailCatalogTest(unittest.TestCase):
    def test_gmail_agent_is_catalog_recipe(self):
        from wactorz.agents.catalog_agent import CatalogAgent

        catalog = CatalogAgent(name="catalog-test", persistence_dir="state/test-gmail-catalog")
        info = catalog._action_info("gmail-agent")

        self.assertTrue(info["ok"])
        self.assertEqual(info["recipe"]["type"], "native")
        self.assertIn("gmail", info["recipe"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
