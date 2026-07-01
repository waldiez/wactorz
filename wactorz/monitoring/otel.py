"""OpenTelemetry integration for Wactorz.

Activated when OTEL_EXPORTER_OTLP_ENDPOINT is set.
Pushes the same per-actor metrics as the Prometheus collector via OTLP HTTP.

Standard OTel env vars (all optional):
  OTEL_EXPORTER_OTLP_ENDPOINT  — e.g. http://localhost:4318  (required to enable)
  OTEL_SERVICE_NAME             — defaults to "wactorz"
  OTEL_EXPORTER_OTLP_HEADERS   — comma-separated "key=value" auth headers
  OTEL_METRIC_EXPORT_INTERVAL  — export interval in ms (default 60000)
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

RegistryProvider = Callable[[], Any | None]

_provider = None  # module-level handle so shutdown() can reach it
_export_warned = False  # suppress repeated connection-error log spam


def setup_otel(registry_provider: RegistryProvider) -> bool:
    """Configure OTel SDK and start periodic metric export.
    Returns True if OTel was successfully set up, False if disabled or unavailable.
    Idempotent — safe to call multiple times.
    """
    global _provider

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return False

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.metrics import Observation
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            MetricExportResult,
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    except ImportError:
        logger.warning("OpenTelemetry packages not installed — run: pip install 'wactorz[otel]'")
        return False

    service_name = os.getenv("OTEL_SERVICE_NAME", "wactorz")
    export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "60000"))

    resource = Resource({SERVICE_NAME: service_name})
    _base_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")

    # Subclass the real exporter so SDK internals (e.g. _preferred_temporality)
    # are inherited without needing to proxy each private attribute.
    class _QuietExporter(OTLPMetricExporter):
        def export(self, metrics_data, **kw) -> MetricExportResult:  # type: ignore[override]
            global _export_warned
            try:
                return super().export(metrics_data, **kw)
            except Exception as exc:
                if not _export_warned:
                    _export_warned = True
                    logger.warning(
                        "OTel export failed (further errors suppressed): %s — "
                        "check OTEL_EXPORTER_OTLP_ENDPOINT in .env",
                        exc,
                    )
                return MetricExportResult.FAILURE

    exporter = _QuietExporter(endpoint=f"{endpoint}/v1/metrics")
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=export_interval_ms)
    _provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = _provider.get_meter("wactorz", version="1.0")

    # ── Observable callbacks ───────────────────────────────────────────────

    def _actors(options):
        registry = registry_provider()
        if registry is None or not hasattr(registry, "all_actors"):
            return
        yield Observation(len(list(registry.all_actors())))

    def _per_actor(attr_fn):
        """Factory: returns a callback that yields one Observation per actor."""

        def _cb(options):
            registry = registry_provider()
            if registry is None or not hasattr(registry, "all_actors"):
                return
            now = time.time()
            for actor in registry.all_actors():
                name = getattr(actor, "name", getattr(actor, "actor_id", "unknown"))
                attrs = {"actor_name": name}
                yield Observation(attr_fn(actor, now), attrs)

        return _cb

    def _up(actor, _now):
        raw = getattr(actor, "state", "unknown")
        return 1 if getattr(raw, "value", str(raw)) == "running" else 0

    def _messages(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "messages_processed", 0))

    def _errors(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "errors", 0))

    def _tasks_ok(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "tasks_completed", 0))

    def _tasks_fail(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "tasks_failed", 0))

    def _restarts(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "restart_count", 0))

    def _uptime(actor, _now):
        m = getattr(actor, "metrics", None)
        return float(getattr(m, "uptime", 0.0)) if m is not None else 0.0

    def _hb_age(actor, now):
        m = getattr(actor, "metrics", None)
        last = float(getattr(m, "last_heartbeat", 0.0)) if m is not None else 0.0
        return max(0.0, now - last) if last else 0.0

    def _input_tokens(actor, _now):
        return float(getattr(actor, "total_input_tokens", 0))

    def _output_tokens(actor, _now):
        return float(getattr(actor, "total_output_tokens", 0))

    def _cost(actor, _now):
        return float(getattr(actor, "total_cost_usd", 0.0))

    # ── Register metrics ──────────────────────────────────────────────────

    meter.create_observable_gauge(
        "wactorz.actors.total",
        callbacks=[_actors],
        description="Number of actors registered in the actor registry.",
        unit="1",
    )
    meter.create_observable_gauge(
        "wactorz.actor.up",
        callbacks=[_per_actor(_up)],
        description="1 if the actor is running, 0 otherwise.",
        unit="1",
    )
    meter.create_observable_counter(
        "wactorz.actor.messages_processed",
        callbacks=[_per_actor(_messages)],
        description="Cumulative messages processed per actor.",
        unit="1",
    )
    meter.create_observable_counter(
        "wactorz.actor.errors",
        callbacks=[_per_actor(_errors)],
        description="Cumulative errors per actor.",
        unit="1",
    )
    meter.create_observable_counter(
        "wactorz.actor.tasks_completed",
        callbacks=[_per_actor(_tasks_ok)],
        description="Cumulative tasks completed per actor.",
        unit="1",
    )
    meter.create_observable_counter(
        "wactorz.actor.tasks_failed",
        callbacks=[_per_actor(_tasks_fail)],
        description="Cumulative tasks failed per actor.",
        unit="1",
    )
    meter.create_observable_gauge(
        "wactorz.actor.restart_count",
        callbacks=[_per_actor(_restarts)],
        description="Supervisor restart count per actor.",
        unit="1",
    )
    meter.create_observable_gauge(
        "wactorz.actor.uptime_seconds",
        callbacks=[_per_actor(_uptime)],
        description="Actor uptime in seconds.",
        unit="s",
    )
    meter.create_observable_gauge(
        "wactorz.actor.heartbeat_age_seconds",
        callbacks=[_per_actor(_hb_age)],
        description="Seconds since the actor last emitted a heartbeat.",
        unit="s",
    )
    meter.create_observable_counter(
        "wactorz.llm.input_tokens",
        callbacks=[_per_actor(_input_tokens)],
        description="Cumulative LLM input tokens per actor.",
        unit="1",
    )
    meter.create_observable_counter(
        "wactorz.llm.output_tokens",
        callbacks=[_per_actor(_output_tokens)],
        description="Cumulative LLM output tokens per actor.",
        unit="1",
    )
    meter.create_observable_gauge(
        "wactorz.llm.cost_usd",
        callbacks=[_per_actor(_cost)],
        description="Cumulative LLM cost in USD per actor.",
        unit="USD",
    )

    logger.info(
        "OpenTelemetry metrics enabled → %s (service=%s, interval=%ds)",
        endpoint,
        service_name,
        export_interval_ms // 1000,
    )
    return True


def shutdown_otel() -> None:
    """Flush and shut down the OTel MeterProvider gracefully."""
    global _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            pass
        _provider = None
