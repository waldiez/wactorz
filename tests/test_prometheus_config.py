import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "prometheus" / "render-config.sh"


class PrometheusConfigTest(unittest.TestCase):
    def _render(self, **env_overrides) -> str:
        env = os.environ.copy()
        env.update(
            {
                "PROMETHEUS_SCRAPE_INTERVAL": "15s",
                "PROMETHEUS_PYTHON_TARGET": "wactorz-python",
                "REST_EXTERNAL_PORT": "8000",
                "PROMETHEUS_MONITOR_MOSQUITTO": "1",
                "PROMETHEUS_MONITOR_FUSEKI": "0",
            }
        )
        env.update(env_overrides)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "prometheus.yml"
            subprocess.run(
                ["sh", str(SCRIPT), str(output_path)],
                check=True,
                cwd=ROOT,
                env=env,
            )
            return output_path.read_text(encoding="utf-8")

    def test_mosquitto_job_enabled_by_default(self):
        rendered = self._render()

        self.assertIn("job_name: mosquitto-blackbox", rendered)
        self.assertNotIn("job_name: fuseki-blackbox", rendered)

    def test_fuseki_job_can_be_enabled(self):
        rendered = self._render(PROMETHEUS_MONITOR_FUSEKI="true")

        self.assertIn("job_name: fuseki-blackbox", rendered)

    def test_optional_jobs_can_be_disabled(self):
        rendered = self._render(
            PROMETHEUS_MONITOR_MOSQUITTO="0",
            PROMETHEUS_MONITOR_FUSEKI="0",
        )

        self.assertNotIn("job_name: mosquitto-blackbox", rendered)
        self.assertNotIn("job_name: fuseki-blackbox", rendered)
