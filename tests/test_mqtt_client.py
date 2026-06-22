"""Unit tests for the central aiomqtt client factory (wactorz.core.mqtt)."""

import sys
import types
import unittest
from unittest import mock


class MqttClientFactoryTest(unittest.TestCase):
    def _call(self, cfg_user, cfg_pass, **kwargs):
        import wactorz.core.mqtt as m

        captured = {}
        fake_aiomqtt = types.SimpleNamespace(
            Client=lambda *a, **k: captured.update(args=a, kwargs=k) or object()
        )
        cfg = types.SimpleNamespace(mqtt_username=cfg_user, mqtt_password=cfg_pass)
        # CONFIG is imported lazily inside mqtt_client() (from ..config), so patch
        # it at the source module, not on wactorz.core.mqtt.
        with mock.patch("wactorz.config.CONFIG", cfg), mock.patch.dict(
            sys.modules, {"aiomqtt": fake_aiomqtt}
        ):
            m.mqtt_client("broker", 1883, **kwargs)
        return captured

    def test_anonymous_when_blank(self):
        # Empty creds (the default) must connect anonymously — no kwargs injected.
        c = self._call("", "")
        self.assertEqual(c["args"], ("broker", 1883))
        self.assertNotIn("username", c["kwargs"])
        self.assertNotIn("password", c["kwargs"])

    def test_credentials_injected_when_set(self):
        c = self._call("alice", "s3cret")
        self.assertEqual(c["kwargs"]["username"], "alice")
        self.assertEqual(c["kwargs"]["password"], "s3cret")

    def test_caller_override_wins(self):
        # An explicit per-call username must not be overwritten by CONFIG.
        c = self._call("alice", "s3cret", username="explicit")
        self.assertEqual(c["kwargs"]["username"], "explicit")

    def test_extra_kwargs_passed_through(self):
        c = self._call("", "", identifier="my-id")
        self.assertEqual(c["kwargs"]["identifier"], "my-id")


if __name__ == "__main__":
    unittest.main()
