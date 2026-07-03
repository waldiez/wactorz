import unittest
from unittest import mock

from wactorz.core.integrations.google_calendar_mcp import (
    GoogleCalendarMcpClient,
    _format_events,
)
from wactorz.core.integrations.google_mcp import _looks_permission_denied


class PermissionDetectionTest(unittest.TestCase):
    def test_detects_permission_denied(self):
        self.assertTrue(_looks_permission_denied("The caller does not have permission"))
        self.assertTrue(_looks_permission_denied("PERMISSION_DENIED: nope"))
        self.assertFalse(_looks_permission_denied("Standup at 10:00"))


class FormatEventsTest(unittest.TestCase):
    def test_formats_timed_and_all_day(self):
        out = _format_events(
            [
                {
                    "summary": "Wactorz MCP",
                    "start": {"dateTime": "2026-07-02T14:00:00+03:00"},
                    "end": {"dateTime": "2026-07-02T18:00:00+03:00"},
                },
                {"summary": "Holiday", "start": {"date": "2026-07-04"}, "end": {"date": "2026-07-05"}},
            ]
        )
        self.assertIn("Wactorz MCP", out)
        self.assertIn("2:00 PM", out)
        self.assertIn("Holiday", out)
        self.assertIn("all day", out)

    def test_empty(self):
        self.assertIn("No events", _format_events([]))


class RestFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_rest_on_permission_denied(self):
        client = GoogleCalendarMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(
            return_value=(
                200,
                {"items": [{"summary": "Thesis", "start": {"dateTime": "2026-07-03T05:45:00+03:00"}, "end": {"dateTime": "2026-07-03T07:45:00+03:00"}}]},
            )
        )

        result = await client.call_tool(
            "list_events", {"startTime": "2026-07-03T00:00:00+03:00", "endTime": "2026-07-04T00:00:00+03:00", "pageSize": 10}
        )

        self.assertIn("Thesis", result)
        method, path = client._rest_request.await_args.args
        self.assertEqual(method, "GET")
        self.assertIn("/calendars/primary/events", path)
        params = client._rest_request.await_args.kwargs["params"]
        self.assertEqual(params["timeMin"], "2026-07-03T00:00:00+03:00")
        self.assertEqual(params["singleEvents"], "true")

    async def test_no_fallback_when_mcp_succeeds(self):
        client = GoogleCalendarMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="Standup at 10:00")
        client._rest_request = mock.AsyncMock()

        result = await client.call_tool("list_events", {"pageSize": 5})

        self.assertEqual(result, "Standup at 10:00")
        client._rest_request.assert_not_awaited()

    async def test_create_event_via_rest_builds_body(self):
        client = GoogleCalendarMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(
            return_value=(200, {"start": {"dateTime": "2026-07-03T08:00:00+03:00"}, "end": {"dateTime": "2026-07-03T14:00:00+03:00"}})
        )

        result = await client.call_tool(
            "create_event",
            {"summary": "sports", "startTime": "2026-07-03T08:00:00+03:00", "endTime": "2026-07-03T14:00:00+03:00"},
        )

        self.assertIn("Created 'sports'", result)
        method, path = client._rest_request.await_args.args
        self.assertEqual(method, "POST")
        body = client._rest_request.await_args.kwargs["json_body"]
        self.assertEqual(body["summary"], "sports")
        self.assertEqual(body["start"]["dateTime"], "2026-07-03T08:00:00+03:00")

    async def test_delete_event_via_rest(self):
        client = GoogleCalendarMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(return_value=(204, {}))

        result = await client.call_tool("delete_event", {"eventId": "abc123"})

        self.assertEqual(result, "Event deleted.")
        method, path = client._rest_request.await_args.args
        self.assertEqual(method, "DELETE")
        self.assertIn("abc123", path)

    async def test_rest_error_surfaced_friendly(self):
        client = GoogleCalendarMcpClient()
        client._call_mcp = mock.AsyncMock(return_value="The caller does not have permission")
        client._rest_request = mock.AsyncMock(
            return_value=(404, {"error": {"message": "Not Found"}})
        )

        result = await client.call_tool("delete_event", {"eventId": "missing"})

        self.assertIn("Not Found", result)


if __name__ == "__main__":
    unittest.main()
