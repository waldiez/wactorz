import importlib.util
import json
import unittest
from unittest import mock

if importlib.util.find_spec("mcp") is None:
    mcp_server = None
    MCP_IMPORT_ERROR = "mcp optional dependency is not installed"
else:
    from wactorz.interfaces import mcp_server

    MCP_IMPORT_ERROR = ""


EXPECTED_TOOLS = {
    "ask_wactorz",
    "ask_agent",
    "list_agents",
    "list_capabilities",
    "stop_agent",
    "pause_agent",
    "resume_agent",
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
        ):
            payload = json.loads(await mcp_server.config_resource())

        self.assertEqual(
            payload,
            {
                "wactorz_url": mcp_server.WACTORZ_URL,
                "wactorz_auth": True,
                "ha_url": "http://ha.local:8123",
                "ha_auth": True,
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("secret-rest-key", serialized)
        self.assertNotIn("secret-ha-token", serialized)

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
                "state": "paused",
                "protected": False,
            },
        ]
        with mock.patch.object(mcp_server, "_wactorz_get", mock.AsyncMock(return_value=agents)):
            result = await mcp_server.list_agents()

        self.assertIn("[running   ] @main [protected]  (id: actor-123)", result)
        self.assertIn("[paused    ] @worker  (id: actor-456)", result)

    async def test_list_agents_surfaces_backend_error(self):
        error = {"error": "Cannot connect to wactorz at http://localhost:8000. Is it running?"}
        with mock.patch.object(mcp_server, "_wactorz_get", mock.AsyncMock(return_value=error)):
            result = await mcp_server.list_agents()

        self.assertEqual(result, error["error"])

    async def test_ha_requires_configuration(self):
        with (
            mock.patch.object(mcp_server, "HA_URL", ""),
            mock.patch.object(mcp_server, "HA_TOKEN", ""),
        ):
            self.assertEqual(
                await mcp_server.ha_list_entities("light"),
                "Home Assistant is not configured. Set HA_URL and HA_TOKEN env vars.",
            )
