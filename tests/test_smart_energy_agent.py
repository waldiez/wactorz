"""Tests for the smart-energy catalog recipe embedded dynamic code."""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "wactorz" / "catalogue_agents" / "smart_energy_agent.py"


def load_agent_namespace():
    src = RECIPE.read_text(encoding="utf-8")
    mod = ast.parse(src)
    code = None
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "AGENT_CODE":
                    code = ast.literal_eval(node.value)
                    break
    if code is None:
        raise AssertionError("AGENT_CODE missing")
    namespace = {}
    exec(compile(code, "smart_energy_agent.AGENT_CODE", "exec"), namespace)
    return namespace


class FakeAgent:
    def __init__(self):
        self.state = {}
        self.persisted = {}
        self.contracts = []
        self.logs = []
        self.published = []
        self.llm = None

    def recall(self, key, default=None):
        return self.persisted.get(key, default)

    def persist(self, key, value):
        self.persisted[key] = value

    def declare_contract(self, **kwargs):
        self.contracts.append(kwargs)

    async def log(self, message, level="info"):
        self.logs.append((level, message))

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


class SmartEnergyRecipeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = load_agent_namespace()
        self.agent = FakeAgent()
        await self.ns["setup"](self.agent)

    def test_embedded_agent_code_compiles(self):
        self.assertIn("handle_task", self.ns)
        self.assertIn("process", self.ns)
        self.assertEqual(len(self.agent.contracts), 1)

    async def test_setup_and_rate_change_persist_defaults(self):
        self.assertEqual(self.agent.state["rate"], 0.138)
        self.assertEqual(self.agent.state["currency"], "EUR")

        result = await self.ns["handle_task"](
            self.agent,
            {"text": "set rate to 0.25 per kwh"},
        )

        self.assertEqual(result["result"], "Rate set to 0.25 EUR/kWh")
        self.assertEqual(self.agent.persisted["rate"], 0.25)
        self.assertEqual(self.agent.persisted["currency"], "EUR")

    def test_discover_candidates_pairs_power_switch_and_energy_meter(self):
        states = {
            "sensor.ac_power": {
                "entity_id": "sensor.ac_power",
                "state": "362",
                "attributes": {
                    "device_class": "power",
                    "unit_of_measurement": "W",
                    "friendly_name": "AC Power",
                },
            },
            "sensor.ac_today_energy": {
                "entity_id": "sensor.ac_today_energy",
                "state": "1.5",
                "attributes": {
                    "device_class": "energy",
                    "unit_of_measurement": "kWh",
                    "friendly_name": "AC Today Energy",
                },
            },
            "switch.ac": {
                "entity_id": "switch.ac",
                "state": "on",
                "attributes": {"friendly_name": "AC"},
            },
        }

        candidates = self.ns["_discover_candidates"](states)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["suggested_name"], "ac")
        self.assertEqual(candidates[0]["switch_entity"], "switch.ac")
        self.assertEqual(candidates[0]["energy_entity"], "sensor.ac_today_energy")
        self.assertEqual(candidates[0]["energy_kind"], "today")

    def test_auto_off_rule_resolves_fuzzy_plug_name_and_keeps_canonical_key(self):
        self.agent.state["plugs"] = {
            "3d_printer": {
                "name": "3d_printer",
                "friendly": "3D Printer",
                "protection": "auto_off_on_idle",
            }
        }

        result = self.ns["_add_rule"](
            self.agent,
            {
                "id": "printer_idle",
                "type": "auto_off_on_idle",
                "plug": "printer",
                "idle_threshold_watts": 20,
            },
        )

        self.assertEqual(result["result"], "Added rule 'printer_idle' (auto_off_on_idle) on plug '3d_printer'")
        self.assertEqual(self.agent.state["rules"]["printer_idle"]["plug"], "3d_printer")
        self.assertEqual(self.agent.persisted["rules"]["printer_idle"]["plug"], "3d_printer")

    def test_auto_off_rule_rejects_locked_plug(self):
        self.agent.state["plugs"] = {
            "ac": {"name": "ac", "friendly": "AC", "protection": "locked"}
        }

        result = self.ns["_add_rule"](
            self.agent,
            {"type": "auto_off_on_idle", "plug": "AC"},
        )

        self.assertEqual(result["result"], "error")
        self.assertIn("locked", result["error"])
        self.assertEqual(self.agent.state["rules"], {})


if __name__ == "__main__":
    unittest.main()