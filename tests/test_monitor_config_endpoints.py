import importlib
import json
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch


def _install_aiohttp_web_stub() -> None:
    class _Response:
        def __init__(self, payload, status=200):
            self.status = status
            self.text = json.dumps(payload)

    web = types.SimpleNamespace(
        json_response=lambda payload, status=200: _Response(payload, status=status),
    )
    sys.modules["aiohttp"] = types.SimpleNamespace(web=web)
    sys.modules["aiohttp.web"] = web


_install_aiohttp_web_stub()


class MonitorConfigEndpointsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._original_env)
        _install_aiohttp_web_stub()
        with (
            patch("dotenv.load_dotenv"),
            patch("dotenv.find_dotenv", return_value=""),
            patch("logging.basicConfig"),
            patch("logging.FileHandler", return_value=logging.NullHandler()),
        ):
            import wactorz.config as config
            importlib.reload(config)
            import wactorz.monitor_server as monitor_server
            importlib.reload(monitor_server)

    def _reload_modules(self):
        _install_aiohttp_web_stub()
        with (
            patch("dotenv.load_dotenv"),
            patch("dotenv.find_dotenv", return_value=""),
            patch("logging.basicConfig"),
            patch("logging.FileHandler", return_value=logging.NullHandler()),
        ):
            import wactorz.config as config
            importlib.reload(config)
            import wactorz.monitor_server as monitor_server
            importlib.reload(monitor_server)
        return config, monitor_server

    async def test_ha_config_handler_returns_centralized_values(self):
        os.environ["HA_URL"] = "http://ha.local:8123/api/"
        os.environ["HA_TOKEN"] = "secret-token"
        config, monitor_server = self._reload_modules()

        self.assertEqual(config.raw_url_target(config.CONFIG.ha_url), "ha.local:8123")

        response = await monitor_server.ha_config_handler(object())

        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.text),
            {
                "url": "ha.local:8123",
                "token": "secret-token",
            },
        )

    async def test_fuseki_config_handler_returns_centralized_values(self):
        os.environ["FUSEKI_URL"] = "ws://192.168.1.200:3030/api/websocket"
        os.environ["FUSEKI_DATASET"] = "/graph"
        os.environ["FUSEKI_USER"] = "admin-user"
        os.environ["FUSEKI_PASSWORD"] = "admin-pass"
        config, monitor_server = self._reload_modules()

        self.assertEqual(config.CONFIG.fuseki_user, "admin-user")
        self.assertEqual(config.CONFIG.fuseki_password, "admin-pass")
        self.assertEqual(config.raw_url_target(config.CONFIG.fuseki_url), "192.168.1.200:3030")

        response = await monitor_server.fuseki_config_handler(object())

        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.text),
            {
                "url": "192.168.1.200:3030",
                "user": "admin-user",
                "password": "admin-pass",
                "dataset": "/graph",
            },
        )
