import importlib
import os
import unittest


class DevModeDefaultsTest(unittest.TestCase):
    def test_dev_mode_defaults_to_rest_and_8080(self):
        original = dict(os.environ)
        try:
            os.environ.pop("INTERFACE", None)
            os.environ.pop("PORT", None)
            os.environ["AGENTFLOW_DEV_MODE"] = "1"
            import config
            importlib.reload(config)
            self.assertEqual(config.CONFIG.interface, "rest")
            self.assertEqual(config.CONFIG.port, 8080)
        finally:
            os.environ.clear()
            os.environ.update(original)
            import config
            importlib.reload(config)

