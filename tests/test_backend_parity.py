import json
import unittest

from tests.backend_parity_harness import FIXTURE_PATH, run_fixtures


class BackendParityTest(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_contract_matches_fixture(self):
        actual = await run_fixtures(FIXTURE_PATH)
        fixture = json.loads(FIXTURE_PATH.read_text())

        expected = {"contract": fixture["contract"], "results": []}
        for scenario in fixture["scenarios"]:
            expected["results"].append(
                {
                    "scenario": scenario["name"],
                    "actors": {
                        name: {
                            "starts": starts,
                            "restart_count": scenario["expected"]["restart_counts"][name],
                            "final_state": scenario["expected"]["final_states"][name],
                        }
                        for name, starts in scenario["expected"]["start_counts"].items()
                    },
                }
            )

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
