import unittest
from unittest import mock

from wactorz.agents.google_calendar_agent import GoogleCalendarAgent


class GoogleCalendarAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_structured_today_lists_events(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")
        agent.client.list_range = mock.AsyncMock(return_value=[
            {"id": "evt-1", "summary": "Standup", "start": "2026-07-03T10:00:00+03:00"}
        ])

        result = await agent._process({"operation": "today", "count": 5})

        self.assertIn("Standup", result["result"])
        self.assertEqual(result["events"][0]["id"], "evt-1")
        agent.client.list_range.assert_awaited_once_with("today", max_results=5)

    async def test_structured_create_event_requires_title_and_start(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")

        result = await agent._process({"operation": "create_event", "summary": "Dentist"})

        self.assertIn("title", result["result"])
        self.assertIn("start", result["missing"])

    async def test_structured_create_event_calls_google_client(self):
        agent = GoogleCalendarAgent(llm_provider=None, persistence_dir="state/test-google-calendar")
        agent.client.create_event = mock.AsyncMock(return_value={
            "id": "evt-2",
            "summary": "Dentist",
            "start": "2026-07-03T15:00:00+03:00",
        })

        result = await agent._process({
            "operation": "create_event",
            "summary": "Dentist",
            "start": "2026-07-03T15:00:00+03:00",
            "location": "Athens",
        })

        self.assertIn("Created calendar event", result["result"])
        agent.client.create_event.assert_awaited_once_with(
            summary="Dentist",
            start="2026-07-03T15:00:00+03:00",
            end="",
            location="Athens",
            description="",
        )


class GoogleCalendarCatalogTest(unittest.TestCase):
    def test_google_calendar_agent_is_catalog_recipe(self):
        from wactorz.agents.catalog_agent import CatalogAgent

        catalog = CatalogAgent(name="catalog-test", persistence_dir="state/test-google-calendar-catalog")
        info = catalog._action_info("google-calendar-agent")

        self.assertTrue(info["ok"])
        self.assertEqual(info["recipe"]["type"], "native")
        self.assertIn("calendar", info["recipe"]["capabilities"])
