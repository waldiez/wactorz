import sys
import time
import types
import unittest


def _install_aiohttp_web_stub() -> None:
    class _Response:
        def __init__(self, *, body=b"", headers=None, content_type=None, status=200):
            self.body = body
            self.status = status
            self.headers = dict(headers or {})
            if content_type is not None:
                self.headers.setdefault("Content-Type", content_type)

    web = types.SimpleNamespace(
        Request=type("Request", (), {}),
        HTTPException=type("HTTPException", (Exception,), {"status": 500}),
        Response=_Response,
        middleware=lambda fn: fn,
    )
    sys.modules["aiohttp"] = types.SimpleNamespace(web=web)


_install_aiohttp_web_stub()

from wactorz.monitoring.prometheus import PrometheusMonitor


class _FakeMetrics:
    def __init__(self):
        self.messages_processed = 7
        self.errors = 2
        self.tasks_completed = 5
        self.tasks_failed = 1
        self.restart_count = 3
        self.start_time = time.time() - 42
        self.last_heartbeat = time.time() - 15

    @property
    def uptime(self):
        return time.time() - self.start_time


class _FakeActor:
    def __init__(self):
        self.actor_id = "actor-123"
        self.name = "main"
        self.protected = True
        self.state = types.SimpleNamespace(value="running")
        self.metrics = _FakeMetrics()
        self.total_input_tokens = 11
        self.total_output_tokens = 13
        self.total_cost_usd = 0.42


class _FakeRegistry:
    def __init__(self, actors):
        self._actors = list(actors)

    def all_actors(self):
        return list(self._actors)


class PrometheusMetricsTest(unittest.TestCase):
    def test_render_contains_actor_and_http_metric_families(self):
        registry = _FakeRegistry([_FakeActor()])
        monitor = PrometheusMonitor(lambda: registry)

        payload = monitor.render().decode()

        self.assertIn("wactorz_actors_total 1.0", payload)
        self.assertIn('wactorz_actor_up{actor_name="main"} 1.0', payload)
        self.assertIn('wactorz_actor_messages_processed_total{actor_name="main"} 7.0', payload)
        self.assertIn('wactorz_actor_tasks_failed_total{actor_name="main"} 1.0', payload)
        self.assertIn('wactorz_llm_cost_usd_total{actor_name="main"} 0.42', payload)
        self.assertIn("wactorz_http_requests_total", payload)

    def test_render_handles_missing_registry(self):
        monitor = PrometheusMonitor(lambda: None)

        payload = monitor.render().decode()

        self.assertIn("wactorz_actors_total 0.0", payload)
