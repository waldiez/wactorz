import unittest
from unittest import mock

from wactorz.agents.google_calendar_agent import GoogleCalendarAgent


class GoogleCalendarAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_structured_today_calls_calendar_mcp(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")
        agent.client.call_tool = mock.AsyncMock(return_value="Standup at 10:00")

        result = await agent._process({"operation": "today", "count": 5})

        self.assertIn("Standup", result["result"])
        tool_name, arguments = agent.client.call_tool.await_args.args
        self.assertEqual(tool_name, "list_events")
        self.assertEqual(arguments["pageSize"], 5)
        self.assertEqual(arguments["orderBy"], "startTime")
        self.assertIn("startTime", arguments)
        self.assertIn("endTime", arguments)

    async def test_structured_create_event_requires_title_start_and_end(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")

        result = await agent._process({"operation": "create_event", "summary": "Dentist"})

        self.assertIn("title", result["result"])
        self.assertIn("start", result["missing"])
        self.assertIn("end", result["missing"])

    async def test_structured_create_event_calls_calendar_mcp(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")
        agent.client.call_tool = mock.AsyncMock(return_value="created")

        result = await agent._process({
            "operation": "create_event",
            "summary": "Dentist",
            "start": "2026-07-03T15:00:00+03:00",
            "end": "2026-07-03T16:00:00+03:00",
            "location": "Athens",
        })

        self.assertEqual(result["result"], "created")
        agent.client.call_tool.assert_awaited_once_with(
            "create_event",
            {
                "summary": "Dentist",
                "startTime": "2026-07-03T15:00:00+03:00",
                "endTime": "2026-07-03T16:00:00+03:00",
                "location": "Athens",
            },
        )


class GoogleCalendarCatalogTest(unittest.TestCase):
    def test_google_calendar_agent_is_catalog_recipe(self):
        from wactorz.agents.catalog_agent import CatalogAgent

        catalog = CatalogAgent(name="catalog-test", persistence_dir="state/test-google-calendar-catalog")
        info = catalog._action_info("google-calendar-agent")

        self.assertTrue(info["ok"])
        self.assertEqual(info["recipe"]["type"], "native")
        self.assertIn("calendar", info["recipe"]["capabilities"])