"""How long an actor's messages wait, and how long it takes over them.

Every actor takes its messages one at a time, so a slow handler shows in its
own time and in the wait of everything queued behind it. Both are timed per
message, leaving out the heartbeats, status pings and stops counted as noise.
"""

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry

from tests.waiting import until
from wactorz.core.actor import Actor, ActorState, Message, MessageType
from wactorz.monitoring import actor_metrics
from wactorz.monitoring.prometheus import PrometheusMonitor


class _Worker(Actor):
    """Takes as long as each test says over a TASK."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.handled: list[Any] = []
        self.release = asyncio.Event()

    async def handle_message(self, message: Message) -> None:
        await self.release.wait()
        self.handled.append(message.payload)


def _worker(tmp_path: Path) -> _Worker:
    # A name of its own: the histograms are shared by every test in the process.
    worker = _Worker(name=f"worker-{uuid.uuid4().hex[:8]}", persistence_dir=str(tmp_path))
    worker.state = ActorState.RUNNING
    return worker


def _sample(name: str, actor_name: str) -> float:
    """One sample of the actor metrics, such as ``..._count``; 0 when not recorded."""
    registry = CollectorRegistry()
    for collector in actor_metrics.COLLECTORS:
        registry.register(collector)
    return registry.get_sample_value(name, {"actor_name": actor_name}) or 0.0


def _count(name: str, actor_name: str) -> float:
    return _sample(f"{name}_count", actor_name)


def _sum(name: str, actor_name: str) -> float:
    return _sample(f"{name}_sum", actor_name)


async def _through_the_loop(worker: _Worker, *messages: Message) -> None:
    """Run the actor's own message loop over ``messages``, then end it."""
    loop = asyncio.create_task(worker._message_loop())
    for message in messages:
        await worker._mailbox.put(message)
    worker.release.set()
    await until(
        lambda: worker._mailbox.empty() and worker._handling_since is None, "the mailbox to drain"
    )
    worker.state = ActorState.STOPPED
    await asyncio.wait({loop}, timeout=5)


def _task(payload: Any = None, *, sent_ago: float = 0.0) -> Message:
    return Message(
        type=MessageType.TASK, sender_id="sender", payload=payload, timestamp=time.time() - sent_ago
    )


class TestTiming:
    async def test_the_wait_counts_from_when_the_message_was_made(self, tmp_path: Path) -> None:
        worker = _worker(tmp_path)

        await _through_the_loop(worker, _task("late", sent_ago=2.0))

        assert _count("wactorz_actor_queue_wait_seconds", worker.name) == 1
        assert _sum("wactorz_actor_queue_wait_seconds", worker.name) >= 2.0

    async def test_the_handler_is_timed(self, tmp_path: Path) -> None:
        worker = _worker(tmp_path)

        await _through_the_loop(worker, _task("one"), _task("two"))

        assert worker.handled == ["one", "two"]
        assert _count("wactorz_actor_message_duration_seconds", worker.name) == 2

    async def test_noise_is_not_timed(self, tmp_path: Path) -> None:
        worker = _worker(tmp_path)
        ping = Message(type=MessageType.HEARTBEAT, sender_id="monitor")

        await _through_the_loop(worker, ping)

        assert _count("wactorz_actor_queue_wait_seconds", worker.name) == 0
        assert _count("wactorz_actor_message_duration_seconds", worker.name) == 0


class TestTheMetricsFrame:
    async def test_it_carries_the_percentiles_once_there_are_messages(self, tmp_path: Path) -> None:
        worker = _worker(tmp_path)
        assert "queue_wait_p50_s" not in worker._build_metrics()

        await _through_the_loop(worker, _task("one", sent_ago=1.0))
        frame = worker._build_metrics()

        assert {
            "queue_wait_p50_s",
            "queue_wait_p95_s",
            "message_p50_s",
            "message_p95_s",
        } <= frame.keys()
        assert frame["queue_wait_p50_s"] >= 1.0


def test_an_actors_series_go_when_forgotten() -> None:
    name = f"worker-{uuid.uuid4().hex[:8]}"
    actor_metrics.QUEUE_WAIT.labels(actor_name=name).observe(1)

    actor_metrics.forget(name)

    assert name not in PrometheusMonitor(lambda: None).render().decode()


def test_prometheus_serves_them() -> None:
    text = PrometheusMonitor(lambda: None).render().decode()

    assert "# TYPE wactorz_actor_queue_wait_seconds histogram" in text
    assert "# TYPE wactorz_actor_message_duration_seconds histogram" in text
