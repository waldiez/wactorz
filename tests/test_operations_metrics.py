"""`/metrics` says what an operator cannot see from the dashboard.

Whether this server is connected to the broker and what is waiting behind that
connection, which edge nodes are up and how long since each was heard from, and
how requests to an LLM provider end and how long they take. Each is read from
where the application already keeps it, when Prometheus scrapes.

The alert rules shipped beside the compose stack are held to the same names.
"""

import asyncio
import re
import threading
import time
import types
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from prometheus_client import CollectorRegistry

from wactorz.agents.llm.base import LLMProvider
from wactorz.agents.llm.retry import ProviderUnavailable
from wactorz.agents.main import MainActor
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.interfaces.chat.rest import RESTInterface
from wactorz.monitoring import llm_metrics
from wactorz.monitoring.prometheus import PrometheusMonitor


def _samples(monitor: PrometheusMonitor) -> dict[str, float]:
    """Every sample `monitor` renders, as `name{labels}` to its value."""
    samples = {}
    for line in monitor.render().decode().splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            samples[name] = float(value)
    return samples


def _no_actors() -> None:
    return None


# ── The broker connection and the outbox ───────────────────────────────────────


class TestTheBrokerConnection:
    def test_before_there_is_a_publisher_it_reads_as_down_and_claims_nothing_else(self) -> None:
        samples = _samples(PrometheusMonitor(_no_actors))

        assert samples["wactorz_mqtt_connected"] == 0
        assert not [name for name in samples if name.startswith("wactorz_mqtt_outbox")]

    def test_the_connection_and_what_waits_behind_it_are_reported(self) -> None:
        publisher = types.SimpleNamespace(
            connected=True,
            queue_depth=3,
            backlog_depth=40,
            publish_failures=5,
            dropped=6,
            discarded=7,
        )

        samples = _samples(PrometheusMonitor(_no_actors, publisher_provider=lambda: publisher))

        assert samples["wactorz_mqtt_connected"] == 1
        assert samples["wactorz_mqtt_outbox_queued"] == 3
        assert samples["wactorz_mqtt_outbox_backlog"] == 40
        assert samples["wactorz_mqtt_publish_failures_total"] == 5
        assert samples["wactorz_mqtt_outbox_dropped_total"] == 6
        assert samples["wactorz_mqtt_outbox_discarded_total"] == 7

    def test_a_real_publisher_reports_itself(self, tmp_path: Path) -> None:
        publisher = MQTTPublisher(db_path=tmp_path / "outbox.db")

        samples = _samples(PrometheusMonitor(_no_actors, publisher_provider=lambda: publisher))

        assert samples["wactorz_mqtt_connected"] == 0
        assert samples["wactorz_mqtt_outbox_queued"] == 0
        assert samples["wactorz_mqtt_outbox_backlog"] == 0


class TestWhatThePublisherCounts:
    """The counters come from the publisher, at the places it already decides."""

    #: A message as the publisher holds one: not stored, so nothing touches disk.
    MESSAGE = ("agents/x/logs", b"{}", False, 0, -1, None)

    def test_a_failed_publish_held_to_retry(self, tmp_path: Path) -> None:
        publisher = MQTTPublisher(db_path=tmp_path / "outbox.db")

        publisher._hold_for_retry(self.MESSAGE, False, OSError("link"))

        assert publisher.publish_failures == 1
        assert publisher.discarded == 0

    def test_a_message_given_up_on(self, tmp_path: Path) -> None:
        publisher = MQTTPublisher(db_path=tmp_path / "outbox.db")

        publisher._discard(self.MESSAGE, False, "it can never be sent")

        assert publisher.discarded == 1

    def test_a_message_that_fails_every_time_is_both(self, tmp_path: Path) -> None:
        publisher = MQTTPublisher(db_path=tmp_path / "outbox.db")

        for _ in range(publisher.POISON_AFTER):
            publisher._hold_for_retry(self.MESSAGE, False, OSError("link"))

        assert publisher.publish_failures == publisher.POISON_AFTER
        assert publisher.discarded == 1


# ── The nodes ──────────────────────────────────────────────────────────────────


def _node(name: str, online: bool, seconds_ago: float, agents: tuple[str, ...]) -> dict[str, Any]:
    return {
        "node": name,
        "online": online,
        "last_seen": time.time() - seconds_ago,
        "agents": list(agents),
        "version": "1.4.2",
        "runtime": "node",
    }


class TestTheNodes:
    def test_a_node_that_is_heard_from_is_up_with_its_age_and_agents(self) -> None:
        monitor = PrometheusMonitor(
            _no_actors, nodes_provider=lambda: [_node("rpi", True, 4, ("a", "b"))]
        )

        samples = _samples(monitor)

        assert samples['wactorz_node_up{node="rpi"}'] == 1
        assert 3 < samples['wactorz_node_heartbeat_age_seconds{node="rpi"}'] < 30
        assert samples['wactorz_node_agents{node="rpi"}'] == 2
        assert samples['wactorz_node_info{node="rpi",runtime="node",version="1.4.2"}'] == 1
        assert samples['wactorz_nodes{state="up"}'] == 1
        assert samples['wactorz_nodes{state="down"}'] == 0

    def test_a_node_gone_quiet_is_down(self) -> None:
        monitor = PrometheusMonitor(
            _no_actors, nodes_provider=lambda: [_node("rpi", False, 70, ())]
        )

        samples = _samples(monitor)

        assert samples['wactorz_node_up{node="rpi"}'] == 0
        assert samples['wactorz_node_heartbeat_age_seconds{node="rpi"}'] > 60

    def test_a_node_this_install_deploys_is_down_until_it_is_heard(self) -> None:
        # Main forgets a node that stays silent, so one that is down would
        # otherwise leave the list, and an alert on it would resolve itself.
        monitor = PrometheusMonitor(
            _no_actors,
            nodes_provider=lambda: [_node("rpi", True, 4, ())],
            expected_nodes_provider=lambda: ["rpi", "attic"],
        )

        samples = _samples(monitor)

        assert samples['wactorz_node_up{node="attic"}'] == 0
        assert 'wactorz_node_heartbeat_age_seconds{node="attic"}' not in samples
        assert samples['wactorz_nodes{state="up"}'] == 1
        assert samples['wactorz_nodes{state="down"}'] == 1

    def test_with_no_nodes_the_totals_are_zero(self) -> None:
        samples = _samples(PrometheusMonitor(_no_actors))

        assert samples['wactorz_nodes{state="up"}'] == 0
        assert samples['wactorz_nodes{state="down"}'] == 0


# ── LLM requests ───────────────────────────────────────────────────────────────


def _llm(provider: str, outcome: str) -> float:
    """How many requests to ``provider`` have ended as ``outcome``."""
    registry = CollectorRegistry()
    for collector in llm_metrics.COLLECTORS:
        registry.register(collector)
    labels = {"provider": provider, "outcome": outcome}
    return registry.get_sample_value("wactorz_llm_requests_total", labels) or 0.0


def _timed(provider: str) -> float:
    registry = CollectorRegistry()
    registry.register(llm_metrics.DURATION)
    labels = {"provider": provider}
    return registry.get_sample_value("wactorz_llm_request_duration_seconds_count", labels) or 0.0


@pytest.fixture(autouse=True)
def _no_spend_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wactorz.agents.llm.base.check_cost_limit", lambda: None)


class _Scripted(LLMProvider):
    """A provider that answers, or fails with what it was given.

    Each test subclasses it under a name of its own: the metric is labelled
    with the provider's class, and the counters live as long as the process.
    """

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure

    async def _complete(self, messages: list[dict], system: str = "", **kwargs: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        return "answer", {}

    async def _complete_with_tools(
        self, messages: list[dict], tools: list[dict[str, Any]], system: str = "", **kwargs: Any
    ) -> Any:
        return await self._complete(messages, system, **kwargs)

    async def _stream(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        yield "ans"
        if self.failure is not None:
            raise self.failure
        yield "wer"


class TestLlmRequests:
    async def test_an_answer_is_counted_and_timed(self) -> None:
        class Answering(_Scripted):
            pass

        await Answering().complete([])
        await Answering().complete_with_tools([], [])

        assert _llm("Answering", "ok") == 2
        assert _timed("Answering") == 2

    async def test_a_failure_is_counted_as_an_error_and_still_raised(self) -> None:
        class Failing(_Scripted):
            pass

        with pytest.raises(ValueError, match="bad request"):
            await Failing(ValueError("bad request")).complete([])

        assert _llm("Failing", "error") == 1
        assert _llm("Failing", "ok") == 0
        assert _timed("Failing") == 1

    async def test_a_provider_that_stayed_unavailable_is_told_apart(self) -> None:
        class Busy(_Scripted):
            pass

        gave_up = ProviderUnavailable("Busy.complete", 3, OSError("refused"))
        with pytest.raises(ProviderUnavailable):
            await Busy(gave_up).complete([])

        assert _llm("Busy", "unavailable") == 1
        assert _llm("Busy", "error") == 0

    async def test_a_stream_is_counted_once_when_it_ends(self) -> None:
        class Streaming(_Scripted):
            pass

        chunks = [chunk async for chunk in Streaming().stream([])]

        assert chunks == ["ans", "wer"]
        assert _llm("Streaming", "ok") == 1
        assert _timed("Streaming") == 1

    async def test_a_stream_that_breaks_is_an_error(self) -> None:
        class Breaking(_Scripted):
            pass

        with pytest.raises(ValueError, match="cut off"):
            async for _chunk in Breaking(ValueError("cut off")).stream([]):
                pass

        assert _llm("Breaking", "error") == 1

    async def test_a_cancelled_request_is_not_counted(self) -> None:
        # Nobody was refused an answer: the caller stopped waiting for one.
        class Cancelled(_Scripted):
            pass

        with pytest.raises(asyncio.CancelledError):
            await Cancelled(asyncio.CancelledError()).complete([])

        assert _timed("Cancelled") == 0


# ── What the REST interface hands the monitor ──────────────────────────────────


class TestTheEndpoint:
    def test_it_reports_the_systems_publisher_and_mains_nodes(self) -> None:
        nodes = types.SimpleNamespace(list_nodes=lambda: [_node("rpi", True, 2, ("a",))])
        main = types.SimpleNamespace(_registry=None, nodes=nodes)
        publisher = types.SimpleNamespace(
            connected=True,
            queue_depth=1,
            backlog_depth=0,
            publish_failures=0,
            dropped=0,
            discarded=0,
        )
        system = types.SimpleNamespace(_mqtt_client=publisher)
        # Metrics never chat, so there is no orchestrator behind this interface.
        interface = RESTInterface(
            cast(Any, None),
            cast(MainActor, main),
            port=0,
            system=system,  # pyright: ignore[reportArgumentType]
        )

        samples = _samples(interface._monitor)

        assert samples["wactorz_mqtt_connected"] == 1
        assert samples["wactorz_mqtt_outbox_queued"] == 1
        assert samples['wactorz_node_up{node="rpi"}'] == 1

    def test_without_a_system_or_a_node_manager_it_still_renders(self) -> None:
        main = types.SimpleNamespace(_registry=None)
        interface = RESTInterface(cast(Any, None), cast(MainActor, main), port=0)

        samples = _samples(interface._monitor)

        assert samples["wactorz_mqtt_connected"] == 0


# ── Agents that keep crashing, and what models cost ────────────────────────────


class TestRestartLoops:
    def test_an_agent_restarted_slowly_here_and_one_on_a_node(self) -> None:
        supervisor = types.SimpleNamespace(slow_retrying=lambda: ["flaky"])
        monitor = PrometheusMonitor(
            _no_actors,
            supervisor_provider=lambda: supervisor,
            nodes_provider=lambda: [{"node": "rpi", "slow_retry": ["camera"]}],
        )

        samples = _samples(monitor)

        assert samples['wactorz_actor_slow_restarts{actor_name="flaky",node=""}'] == 1
        assert samples['wactorz_actor_slow_restarts{actor_name="camera",node="rpi"}'] == 1

    def test_without_a_supervisor_or_nodes_there_is_nothing_to_report(self) -> None:
        rendered = PrometheusMonitor(_no_actors).render().decode()

        assert "# TYPE wactorz_actor_slow_restarts gauge" in rendered
        assert not [k for k in _samples(PrometheusMonitor(_no_actors)) if "slow_restarts" in k]


class TestSpend:
    async def test_the_spend_and_its_limit_are_read_before_rendering(self) -> None:
        info = {"period": "monthly", "spend_usd": 4.2, "limit_usd": 5.0}
        monitor = PrometheusMonitor(_no_actors, spend_provider=lambda: info)

        await monitor.refresh()
        samples = _samples(monitor)

        assert samples['wactorz_llm_spend_usd{period="monthly"}'] == 4.2
        assert samples['wactorz_llm_spend_limit_usd{period="monthly"}'] == 5.0

    async def test_no_limit_reports_the_spend_alone(self) -> None:
        monitor = PrometheusMonitor(
            _no_actors,
            spend_provider=lambda: {"period": "daily", "spend_usd": 0.5, "limit_usd": None},
        )

        await monitor.refresh()
        samples = _samples(monitor)

        assert samples['wactorz_llm_spend_usd{period="daily"}'] == 0.5
        assert not [k for k in samples if k.startswith("wactorz_llm_spend_limit_usd")]

    async def test_a_read_that_fails_leaves_the_spend_unreported(self) -> None:
        def broken() -> dict[str, Any]:
            raise RuntimeError("database is locked")

        monitor = PrometheusMonitor(_no_actors, spend_provider=broken)

        await monitor.refresh()

        assert not [k for k in _samples(monitor) if k.startswith("wactorz_llm_spend")]

    async def test_the_read_is_made_off_the_event_loop(self) -> None:
        # It reads the database, and the loop is every agent's.
        seen: list[int] = []

        def provider() -> dict[str, Any]:
            seen.append(threading.get_ident())
            return {}

        await PrometheusMonitor(_no_actors, spend_provider=provider).refresh()

        assert seen and seen[0] != threading.get_ident()


# ── The alert rules ────────────────────────────────────────────────────────────

ALERTS = Path(__file__).resolve().parents[1] / "infra" / "prometheus" / "alerts.yml"

_WACTORZ_METRIC = re.compile(r"\bwactorz_\w+")


def _exported() -> set[str]:
    """Every metric name `/metrics` can carry, whether or not it has a sample yet."""
    llm_metrics.record("Alerting", llm_metrics.OK, 0.1)
    publisher = types.SimpleNamespace(connected=True)
    monitor = PrometheusMonitor(_no_actors, publisher_provider=lambda: publisher)
    names = set()
    for line in monitor.render().decode().splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()[:4]
            names.add(name)
            # A counter's samples are named `<name>_total`, and a family with no
            # samples yet (no actors here) shows only its declaration.
            if kind == "counter":
                names.add(f"{name}_total")
        elif line and not line.startswith("#"):
            names.add(re.split(r"[{ ]", line, maxsplit=1)[0])
    return names


def _rules() -> list[dict[str, Any]]:
    groups = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))["groups"]
    return [rule for group in groups for rule in group["rules"]]


class TestTheAlertRules:
    def test_every_metric_a_rule_reads_is_one_the_app_exports(self) -> None:
        # A rule on a metric that was renamed or never existed is not an error
        # to Prometheus: it evaluates to nothing, and the alert never fires.
        exported = _exported()

        for rule in _rules():
            for name in _WACTORZ_METRIC.findall(rule["expr"]):
                assert name in exported, f"{rule['alert']} reads {name}, which /metrics lacks"

    @pytest.mark.parametrize(
        "metric",
        [
            "wactorz_mqtt_connected",
            "wactorz_mqtt_outbox_backlog",
            "wactorz_mqtt_outbox_dropped_total",
            "wactorz_node_up",
            "wactorz_llm_requests_total",
            "wactorz_actor_handling_seconds",
            "wactorz_event_loop_lag_seconds",
            "wactorz_actor_errors_total",
            "wactorz_actor_slow_restarts",
            "wactorz_llm_spend_usd",
            "wactorz_llm_spend_limit_usd",
        ],
    )
    def test_what_this_file_adds_to_metrics_has_a_rule(self, metric: str) -> None:
        assert any(metric in rule["expr"] for rule in _rules())

    def test_every_rule_says_how_bad_it_is_and_what_it_means(self) -> None:
        for rule in _rules():
            assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]
            assert rule["annotations"]["description"], rule["alert"]
