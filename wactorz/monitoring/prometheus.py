"""Prometheus integration for the Python Wactorz runtime."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector


RegistryProvider = Callable[[], Any | None]


class ActorMetricsCollector:
    """Collects actor and LLM metrics from the live registry."""

    def __init__(self, registry_provider: RegistryProvider):
        self._registry_provider = registry_provider

    def collect(self) -> Iterable[GaugeMetricFamily | CounterMetricFamily]:
        registry = self._registry_provider()
        if registry is not None and hasattr(registry, "all_actors"):
            actors = list(registry.all_actors())
        else:
            actors = []
        now = time.time()

        actors_total = GaugeMetricFamily(
            "wactorz_actors_total",
            "Number of actors currently registered in the Python actor registry.",
        )
        actors_total.add_metric([], len(actors))
        yield actors_total

        actors_by_state = GaugeMetricFamily(
            "wactorz_actors_by_state",
            "Number of registered actors grouped by actor state.",
            labels=["state"],
        )
        actor_info = GaugeMetricFamily(
            "wactorz_actor_info",
            "Static labels describing each registered actor.",
            labels=["actor_name", "actor_class", "protected"],
        )
        actor_up = GaugeMetricFamily(
            "wactorz_actor_up",
            "Whether an actor is currently running.",
            labels=["actor_name"],
        )
        actor_state = GaugeMetricFamily(
            "wactorz_actor_state",
            "Actor state as a labelled gauge for PromQL filtering.",
            labels=["actor_name", "state"],
        )
        actor_messages_processed = CounterMetricFamily(
            "wactorz_actor_messages_processed",
            "Messages processed by each actor.",
            labels=["actor_name"],
        )
        actor_errors = CounterMetricFamily(
            "wactorz_actor_errors",
            "Errors recorded by each actor.",
            labels=["actor_name"],
        )
        actor_tasks_completed = CounterMetricFamily(
            "wactorz_actor_tasks_completed",
            "Tasks completed by each actor.",
            labels=["actor_name"],
        )
        actor_tasks_failed = CounterMetricFamily(
            "wactorz_actor_tasks_failed",
            "Tasks failed by each actor.",
            labels=["actor_name"],
        )
        actor_restarts = GaugeMetricFamily(
            "wactorz_actor_restart_count",
            "Supervisor restart count for each actor.",
            labels=["actor_name"],
        )
        actor_uptime = GaugeMetricFamily(
            "wactorz_actor_uptime_seconds",
            "Actor uptime in seconds.",
            labels=["actor_name"],
        )
        actor_heartbeat_age = GaugeMetricFamily(
            "wactorz_actor_heartbeat_age_seconds",
            "Seconds since the actor last emitted a heartbeat.",
            labels=["actor_name"],
        )
        llm_input_tokens = CounterMetricFamily(
            "wactorz_llm_input_tokens",
            "Total LLM input tokens consumed by each actor.",
            labels=["actor_name"],
        )
        llm_output_tokens = CounterMetricFamily(
            "wactorz_llm_output_tokens",
            "Total LLM output tokens produced by each actor.",
            labels=["actor_name"],
        )
        llm_cost = CounterMetricFamily(
            "wactorz_llm_cost_usd",
            "Total LLM cost in USD for each actor.",
            labels=["actor_name"],
        )

        state_counts: dict[str, int] = {}
        for actor in actors:
            actor_name = getattr(actor, "name", getattr(actor, "actor_id", "unknown"))
            actor_class = actor.__class__.__name__
            protected = "true" if bool(getattr(actor, "protected", False)) else "false"
            raw_state = getattr(actor, "state", "unknown")
            state_value = getattr(raw_state, "value", str(raw_state))
            metrics = getattr(actor, "metrics", None)
            messages_processed = float(getattr(metrics, "messages_processed", 0))
            errors = float(getattr(metrics, "errors", 0))
            tasks_completed = float(getattr(metrics, "tasks_completed", 0))
            tasks_failed = float(getattr(metrics, "tasks_failed", 0))
            restart_count = float(getattr(metrics, "restart_count", 0))
            uptime = float(getattr(metrics, "uptime", 0.0)) if metrics is not None else 0.0
            last_heartbeat = float(getattr(metrics, "last_heartbeat", 0.0)) if metrics is not None else 0.0
            heartbeat_age = max(0.0, now - last_heartbeat) if last_heartbeat else 0.0

            actor_info.add_metric([actor_name, actor_class, protected], 1)
            actor_up.add_metric([actor_name], 1 if state_value == "running" else 0)
            actor_state.add_metric([actor_name, state_value], 1)
            actor_messages_processed.add_metric([actor_name], messages_processed)
            actor_errors.add_metric([actor_name], errors)
            actor_tasks_completed.add_metric([actor_name], tasks_completed)
            actor_tasks_failed.add_metric([actor_name], tasks_failed)
            actor_restarts.add_metric([actor_name], restart_count)
            actor_uptime.add_metric([actor_name], uptime)
            actor_heartbeat_age.add_metric([actor_name], heartbeat_age)

            llm_input_tokens.add_metric([actor_name], float(getattr(actor, "total_input_tokens", 0)))
            llm_output_tokens.add_metric([actor_name], float(getattr(actor, "total_output_tokens", 0)))
            llm_cost.add_metric([actor_name], float(getattr(actor, "total_cost_usd", 0.0)))
            state_counts[state_value] = state_counts.get(state_value, 0) + 1

        for state_name, count in sorted(state_counts.items()):
            actors_by_state.add_metric([state_name], count)

        yield actors_by_state
        yield actor_info
        yield actor_up
        yield actor_state
        yield actor_messages_processed
        yield actor_errors
        yield actor_tasks_completed
        yield actor_tasks_failed
        yield actor_restarts
        yield actor_uptime
        yield actor_heartbeat_age
        yield llm_input_tokens
        yield llm_output_tokens
        yield llm_cost


class PrometheusMonitor:
    """Owns Prometheus metrics and HTTP instrumentation for the REST API."""

    def __init__(self, registry_provider: RegistryProvider):
        self._registry = CollectorRegistry(auto_describe=True)
        self._actor_collector = ActorMetricsCollector(registry_provider)
        self._registry.register(self._actor_collector)
        ProcessCollector(registry=self._registry)
        PlatformCollector(registry=self._registry)

        from prometheus_client import Counter, Histogram

        self._requests_total = Counter(
            "wactorz_http_requests_total",
            "HTTP requests received by the Python REST interface.",
            labelnames=("method", "route"),
            registry=self._registry,
        )
        self._responses_total = Counter(
            "wactorz_http_responses_total",
            "HTTP responses returned by the Python REST interface.",
            labelnames=("method", "route", "status"),
            registry=self._registry,
        )
        self._request_duration_seconds = Histogram(
            "wactorz_http_request_duration_seconds",
            "HTTP request duration for the Python REST interface.",
            labelnames=("method", "route"),
            registry=self._registry,
        )

    @staticmethod
    def _route_label(request: web.Request) -> str:
        route = getattr(request.match_info, "route", None)
        resource = getattr(route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if canonical:
            return canonical
        return request.path

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        route = self._route_label(request)
        method = request.method
        start = time.perf_counter()
        status = 500
        self._requests_total.labels(method=method, route=route).inc()
        try:
            response = await handler(request)
            status = getattr(response, "status", 200)
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            duration = time.perf_counter() - start
            self._responses_total.labels(method=method, route=route, status=str(status)).inc()
            self._request_duration_seconds.labels(method=method, route=route).observe(duration)

    def render(self) -> bytes:
        return generate_latest(self._registry)

    def metrics_response(self) -> web.Response:
        return web.Response(
            body=self.render(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )
