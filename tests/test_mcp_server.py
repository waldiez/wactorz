import importlib.util
import json
import sys
import unittest
from unittest import mock

if importlib.util.find_spec("mcp") is None:
    mcp_server = None
    MCP_IMPORT_ERROR = "mcp optional dependency is not installed"
else:
    aiohttp_module = sys.modules.get("aiohttp")
    if aiohttp_module is not None and not hasattr(aiohttp_module, "__path__"):
        sys.modules.pop("aiohttp", None)
    from wactorz.interfaces import mcp_server

    MCP_IMPORT_ERROR = ""


EXPECTED_TOOLS = {
    "ask_wactorz",
    "ask_agent",
    "calendar_status",
    "calendar_mcp_list_tools",
    "calendar_mcp_call_tool",
    "calendar_list",
    "calendar_today",
    "calendar_week",
    "calendar_create_event",
    "calendar_delete_event",
    "list_agents",
    "list_capabilities",
    "stop_agent",
    "ha_list_entities",
    "ha_get_state",
    "ha_call_service",
}

EXPECTED_RESOURCES = {
    "wactorz://agents",
    "wactorz://capabilities",
    "wactorz://ha-map",
    "wactorz://config",
}


@unittest.skipUnless(mcp_server, MCP_IMPORT_ERROR or "mcp optional dependency is not installed")
class McpServerContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_expected_tools_are_registered(self):
        tools = await mcp_server.mcp.list_tools()
        names = {tool.name for tool in tools}
        self.assertTrue(EXPECTED_TOOLS.issubset(names))

    async def test_expected_resources_are_registered(self):
        resources = await mcp_server.mcp.list_resources()
        uris = {str(resource.uri) for resource in resources}
        self.assertTrue(EXPECTED_RESOURCES.issubset(uris))

    async def test_config_resource_sanitizes_tokens(self):
        with (
            mock.patch.object(mcp_server, "WACTORZ_API_KEY", "secret-rest-key"),
            mock.patch.object(mcp_server, "HA_TOKEN", "secret-ha-token"),
            mock.patch.object(mcp_server, "HA_URL", "http://ha.local:8123"),
            mock.patch.object(mcp_server, "CALENDAR_MCP_URL", "https://calendar.example/mcp"),
            mock.patch.object(mcp_server, "CALENDAR_MCP_TOKEN", ""),
            mock.patch.object(mcp_server, "CALENDAR_MCP_AUTHORIZATION", ""),
            mock.patch.object(
                mcp_server, "CALENDAR_MCP_CLIENT_ID", "client-id.apps.googleusercontent.com"
            ),
            mock.patch.object(mcp_server, "CALENDAR_MCP_CLIENT_SECRET", "secret-client-secret"),
        ):
            payload = json.loads(await mcp_server.config_resource())

        self.assertEqual(payload["wactorz_url"], mcp_server.WACTORZ_URL)
        self.assertTrue(payload["wactorz_auth"])
        self.assertEqual(payload["ha_url"], "http://ha.local:8123")
        self.assertTrue(payload["ha_auth"])
        self.assertEqual(payload["calendar_mcp_url"], "https://calendar.example/mcp")
        self.assertTrue(payload["calendar_mcp_auth"])
        self.assertTrue(payload["calendar_mcp_oauth_client"])
        serialized = json.dumps(payload)
        self.assertNotIn("secret-rest-key", serialized)
        self.assertNotIn("secret-ha-token", serialized)
        self.assertNotIn("secret-client-secret", serialized)

    async def test_list_agents_formats_running_agents(self):
        agents = [
            {
                "id": "actor-123",
                "name": "main",
                "state": "running",
                "protected": True,
            },
            {
                "id": "actor-456",
                "name": "worker",
                "state": "stopped",
                "protected": False,
            },
        ]
        with mock.patch.object(mcp_server, "_wactorz_get", mock.AsyncMock(return_value=agents)):
            result = await mcp_server.list_agents()

        self.assertIn("[running   ] @main [protected]  (id: actor-123)", result)
        self.assertIn("[stopped   ] @worker  (id: actor-456)", result)

    async def test_list_agents_surfaces_backend_error(self):
        error = {"error": "Cannot connect to wactorz at http://localhost:8000. Is it running?"}
        with mock.patch.object(mcp_server, "_wactorz_get", mock.AsyncMock(return_value=error)):
            result = await mcp_server.list_agents()

        self.assertEqual(result, error["error"])

    async def test_calendar_status_sanitizes_remote_mcp_auth(self):
        with (
            mock.patch.object(mcp_server, "CALENDAR_MCP_URL", "https://calendar.example/mcp"),
            mock.patch.object(mcp_server, "CALENDAR_MCP_TOKEN", ""),
            mock.patch.object(mcp_server, "CALENDAR_MCP_AUTHORIZATION", ""),
            mock.patch.object(
                mcp_server, "CALENDAR_MCP_CLIENT_ID", "client-id.apps.googleusercontent.com"
            ),
            mock.patch.object(mcp_server, "CALENDAR_MCP_CLIENT_SECRET", "secret-client-secret"),
            mock.patch.object(
                mcp_server, "CALENDAR_MCP_REDIRECT_URI", "http://localhost:8765/oauth/callback"
            ),
            mock.patch.object(mcp_server, "CALENDAR_MCP_TOKEN_FILE", "C:/tmp/calendar_token.json"),
        ):
            payload = json.loads(await mcp_server.calendar_status())

        self.assertEqual(payload["calendar_mcp_url"], "https://calendar.example/mcp")
        self.assertTrue(payload["calendar_mcp_auth"])
        self.assertTrue(payload["calendar_mcp_oauth_client"])
        self.assertEqual(
            payload["calendar_mcp_redirect_uri"], "http://localhost:8765/oauth/callback"
        )
        self.assertEqual(payload["calendar_mcp_token_file"], "C:/tmp/calendar_token.json")
        self.assertNotIn("secret-client-secret", json.dumps(payload))

    async def test_calendar_list_calls_remote_mcp_list_events(self):
        with mock.patch.object(
            mcp_server,
            "_calendar_mcp_call",
            mock.AsyncMock(return_value="event list"),
        ) as call:
            result = await mcp_server.calendar_list(5)

        self.assertEqual(result, "event list")
        call.assert_awaited_once_with("list_events", {"pageSize": 5, "orderBy": "startTime"})

    async def test_calendar_create_event_calls_remote_mcp_create_event(self):
        with mock.patch.object(
            mcp_server,
            "_calendar_mcp_call",
            mock.AsyncMock(return_value="created"),
        ) as call:
            result = await mcp_server.calendar_create_event(
                "Dentist",
                "2026-06-03T15:00:00+03:00",
                "2026-06-03T16:00:00+03:00",
                location="Athens",
            )

        self.assertEqual(result, "created")
        call.assert_awaited_once_with(
            "create_event",
            {
                "summary": "Dentist",
                "startTime": "2026-06-03T15:00:00+03:00",
                "endTime": "2026-06-03T16:00:00+03:00",
                "location": "Athens",
            },
        )

    async def test_calendar_create_event_requires_end_time(self):
        with mock.patch.object(
            mcp_server,
            "_calendar_mcp_call",
            mock.AsyncMock(return_value="created"),
        ) as call:
            result = await mcp_server.calendar_create_event(
                "Dentist",
                "2026-06-03T15:00:00+03:00",
            )

        self.assertIn("requires", result)
        call.assert_not_called()

    async def test_calendar_mcp_call_tool_validates_json_object(self):
        self.assertIn("Invalid arguments_json", await mcp_server.calendar_mcp_call_tool("x", "{"))
        self.assertEqual(
            await mcp_server.calendar_mcp_call_tool("x", "[]"),
            "arguments_json must encode a JSON object.",
        )

    async def test_ha_requires_configuration(self):
        with (
            mock.patch.object(mcp_server, "HA_URL", ""),
            mock.patch.object(mcp_server, "HA_TOKEN", ""),
        ):
            self.assertEqual(
                await mcp_server.ha_list_entities("light"),
                "Home Assistant is not configured. Set HA_URL and HA_TOKEN env vars.",
            )
