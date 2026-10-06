"""Prometheus integration for the Python Wactorz runtime."""

import time
from collections.abc import Callable, Iterable
from typing import Any

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

from . import agent_metrics, llm_metrics, loop_lag

RegistryProvider = Callable[[], Any | None]

#: The broker publisher, or None before the system has one.
PublisherProvider = Callable[[], Any | None]

#: Every node main knows, as `NodeManager.list_nodes` describes them.
NodesProvider = Callable[[], list[dict[str, Any]]]

#: The names of the nodes this install is configured to deploy.
ExpectedNodesProvider = Callable[[], Iterable[str]]

#: Route label for requests that matched no route in the routing table.
UNMATCHED_ROUTE = "<unmatched>"


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
            "Tasks failed by each actor, the ones that timed out among them.",
            labels=["actor_name"],
        )
        actor_tasks_timed_out = CounterMetricFamily(
            "wactorz_actor_tasks_timed_out",
            "Tasks each actor was still running when their time ran out.",
            labels=["actor_name"],
        )
        actor_messages_refused = CounterMetricFamily(
            "wactorz_actor_messages_refused",
            "Messages an actor's mailbox had no room for: notifications dropped, others refused.",
            labels=["actor_name"],
        )
        actor_mailbox_depth = GaugeMetricFamily(
            "wactorz_actor_mailbox_depth",
            "Messages waiting in each actor's mailbox.",
            labels=["actor_name"],
        )
        actor_handling = GaugeMetricFamily(
            "wactorz_actor_handling_seconds",
            "How long each actor has been on the message it is handling; 0 when idle.",
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
            last_heartbeat = (
                float(getattr(metrics, "last_heartbeat", 0.0)) if metrics is not None else 0.0
            )
            heartbeat_age = max(0.0, now - last_heartbeat) if last_heartbeat else 0.0

            actor_info.add_metric([actor_name, actor_class, protected], 1)
            actor_up.add_metric([actor_name], 1 if state_value == "running" else 0)
            actor_state.add_metric([actor_name, state_value], 1)
            actor_messages_processed.add_metric([actor_name], messages_processed)
            actor_errors.add_metric([actor_name], errors)
            actor_tasks_completed.add_metric([actor_name], tasks_completed)
            actor_tasks_failed.add_metric([actor_name], tasks_failed)
            actor_tasks_timed_out.add_metric(
                [actor_name], float(getattr(metrics, "tasks_timed_out", 0))
            )
            actor_messages_refused.add_metric(
                [actor_name], float(getattr(metrics, "messages_refused", 0))
            )
            actor_handling.add_metric([actor_name], float(getattr(actor, "handling_seconds", 0.0)))
            mailbox = getattr(actor, "_mailbox", None)
            if mailbox is not None:
                actor_mailbox_depth.add_metric([actor_name], float(mailbox.qsize()))
            actor_restarts.add_metric([actor_name], restart_count)
            actor_uptime.add_metric([actor_name], uptime)
            actor_heartbeat_age.add_metric([actor_name], heartbeat_age)

            llm_input_tokens.add_metric(
                [actor_name], float(getattr(actor, "total_input_tokens", 0))
            )
            llm_output_tokens.add_metric(
                [actor_name], float(getattr(actor, "total_output_tokens", 0))
            )
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
        yield actor_tasks_timed_out
        yield actor_messages_refused
        yield actor_mailbox_depth
        yield actor_handling
        yield actor_restarts
        yield actor_uptime
        yield actor_heartbeat_age
        yield llm_input_tokens
        yield llm_output_tokens
        yield llm_cost


class BrokerMetricsCollector:
    """The state of this server's broker connection and of the outbox behind it.

    Read from the publisher when Prometheus scrapes, so it costs nothing in
    between. Before the system has a publisher the connection reads as down
    and the rest is left out, since zero would claim an empty outbox nobody
    has looked at.
    """

    def __init__(self, publisher_provider: PublisherProvider):
        self._publisher_provider = publisher_provider

    def collect(self) -> Iterable[GaugeMetricFamily | CounterMetricFamily]:
        publisher = self._publisher_provider()
        connected = GaugeMetricFamily(
            "wactorz_mqtt_connected",
            "Whether this server's connection to the MQTT broker is up.",
        )
        connected.add_metric([], 1 if getattr(publisher, "connected", False) else 0)
        yield connected
        if publisher is None:
            return

        gauges = (
            (
                "wactorz_mqtt_outbox_queued",
                "Messages in memory waiting to be sent to the broker.",
                "queue_depth",
            ),
            (
                "wactorz_mqtt_outbox_backlog",
                "Stored messages waiting on disk for room in the in-memory queue.",
                "backlog_depth",
            ),
        )
        for name, help_text, attribute in gauges:
            gauge = GaugeMetricFamily(name, help_text)
            gauge.add_metric([], float(getattr(publisher, attribute, 0)))
            yield gauge

        counters = (
            (
                "wactorz_mqtt_publish_failures",
                "Publishes that failed on a live connection and were held to retry.",
                "publish_failures",
            ),
            (
                "wactorz_mqtt_outbox_dropped",
                "Messages discarded because the outbox was full.",
                "dropped",
            ),
            (
                "wactorz_mqtt_outbox_discarded",
                "Messages given up on: unsendable, expired undelivered, or failing every try.",
                "discarded",
            ),
        )
        for name, help_text, attribute in counters:
            counter = CounterMetricFamily(name, help_text)
            counter.add_metric([], float(getattr(publisher, attribute, 0)))
            yield counter


class NodeMetricsCollector:
    """The edge nodes: which are up, how long since each was heard, what each runs.

    Main forgets a node that has been silent for a while, so a node that is
    down would simply vanish from a list of the ones it knows. The nodes this
    install is configured to deploy are therefore reported whether or not they
    have been heard from, as down, which is what an alert can be written on.
    """

    def __init__(self, nodes_provider: NodesProvider, expected_provider: ExpectedNodesProvider):
        self._nodes_provider = nodes_provider
        self._expected_provider = expected_provider

    def collect(self) -> Iterable[GaugeMetricFamily]:
        known = {str(node.get("node")): node for node in self._nodes_provider()}
        names = sorted({*known, *self._expected_provider()})
        now = time.time()

        total = GaugeMetricFamily(
            "wactorz_nodes",
            "Edge nodes this server deploys or has heard from, by whether they are up.",
            labels=["state"],
        )
        up = GaugeMetricFamily(
            "wactorz_node_up",
            "Whether an edge node's heartbeat is recent.",
            labels=["node"],
        )
        age = GaugeMetricFamily(
            "wactorz_node_heartbeat_age_seconds",
            "Seconds since an edge node's last heartbeat.",
            labels=["node"],
        )
        agents = GaugeMetricFamily(
            "wactorz_node_agents",
            "Agents an edge node reported running in its last heartbeat.",
            labels=["node"],
        )
        info = GaugeMetricFamily(
            "wactorz_node_info",
            "The version and runtime an edge node reported.",
            labels=["node", "version", "runtime"],
        )

        online = 0
        for name in names:
            node = known.get(name)
            is_up = bool(node and node.get("online"))
            online += is_up
            up.add_metric([name], 1 if is_up else 0)
            if node is None:
                continue
            last_seen = float(node.get("last_seen") or 0)
            if last_seen:
                age.add_metric([name], max(0.0, now - last_seen))
            agents.add_metric([name], len(node.get("agents") or []))
            info.add_metric(
                [name, str(node.get("version") or "unknown"), str(node.get("runtime") or "")], 1
            )
        total.add_metric(["up"], online)
        total.add_metric(["down"], len(names) - online)

        yield total
        yield up
        yield age
        yield agents
        yield info


def _no_publisher() -> None:
    return None


def _no_nodes() -> list[dict[str, Any]]:
    return []


def _no_names() -> Iterable[str]:
    return ()


class PrometheusMonitor:
    """Owns Prometheus metrics and HTTP instrumentation for the REST API."""

    def __init__(
        self,
        registry_provider: RegistryProvider,
        publisher_provider: PublisherProvider = _no_publisher,
        nodes_provider: NodesProvider = _no_nodes,
        expected_nodes_provider: ExpectedNodesProvider = _no_names,
    ):
        self._registry = CollectorRegistry(auto_describe=True)
        self._actor_collector = ActorMetricsCollector(registry_provider)
        self._registry.register(self._actor_collector)
        self._registry.register(BrokerMetricsCollector(publisher_provider))
        self._registry.register(NodeMetricsCollector(nodes_provider, expected_nodes_provider))
        for collector in (
            *llm_metrics.COLLECTORS,
            *loop_lag.COLLECTORS,
            *agent_metrics.COLLECTORS,
        ):
            self._registry.register(collector)
        ProcessCollector(registry=self._registry)
        PlatformCollector(registry=self._registry)

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
        """Registered route pattern for a request, or a constant when none matched.

        The label must come from the routing table, never from the request line:
        an unrouted path is caller-supplied, so returning it would let anyone
        open a new time series per request and grow the metric without bound.
        """
        route = getattr(request.match_info, "route", None)
        resource = getattr(route, "resource", None)
        canonical = getattr(resource, "canonical", None)
        if canonical:
            return canonical
        return UNMATCHED_ROUTE

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
