"""The monitor watches, classifies and tells the user. It never restarts anything.

The Supervisor is the single restart authority, so everything the monitor does
ends in a published alert or a message to the orchestrator. These pin which
events reach the user, how often, and what the health frame reports.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import monitor_agent
from wactorz.agents.monitor_agent import _NOTIFY_COOLDOWN, MonitorActor
from wactorz.core.actor import Actor, ActorState, Message, MessageType
from wactorz.core.registry import ActorRegistry


class _Worker(Actor):
    """A registered actor the monitor can observe."""

    _consecutive_errors: int = 0

    async def handle_message(self, msg: Message) -> None:
        return None


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload))

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.published]


class _Setup:
    """A monitor on a real registry, with a stand-in orchestrator to notify."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.registry = ActorRegistry()
        self.monitor = MonitorActor(persistence_dir=str(tmp_path))
        self.broker = _Broker()
        self.monitor._mqtt_client = self.broker
        self.main = _Worker(name="main", persistence_dir=str(tmp_path))
        self._tmp_path = tmp_path
        monkeypatch.setattr(monitor_agent, "find_main_actor", self._find_main)
        self.has_main = True

    def _find_main(self, registry: Any) -> Any:
        return self.main if self.has_main else None

    async def register(self) -> None:
        await self.registry.register(self.monitor)
        await self.registry.register(self.main)

    async def worker(self, name: str) -> _Worker:
        actor = _Worker(name=name, persistence_dir=str(self._tmp_path))
        await self.registry.register(actor)
        return actor

    def notifications(self) -> list[dict[str, Any]]:
        found = []
        while not self.main._mailbox.empty():
            msg = self.main._mailbox.get_nowait()
            if isinstance(msg.payload, dict) and msg.payload.get("_monitor_notification"):
                found.append(msg.payload)
        return found


@pytest.fixture(name="setup")
async def setup_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Setup:
    setup = _Setup(tmp_path, monkeypatch)
    await setup.register()
    return setup


def _error_event(actor_id: str = "a" * 36, **overrides: Any) -> dict[str, Any]:
    event = {
        "_monitor_error_event": True,
        "actor_id": actor_id,
        "name": "sensor",
        "phase": "loop",
        "error": "boom",
        "severity": "warning",
        "consecutive": 1,
    }
    event.update(overrides)
    return event


class TestConstruction:
    def test_it_is_named_monitor_and_protected(self, tmp_path: Path) -> None:
        monitor = MonitorActor(persistence_dir=str(tmp_path))

        assert monitor.name == "monitor"
        assert monitor.protected

    def test_a_name_given_by_the_caller_is_kept(self, tmp_path: Path) -> None:
        assert MonitorActor(name="watcher", persistence_dir=str(tmp_path)).name == "watcher"


class TestOnStart:
    async def test_every_other_actor_starts_as_just_seen(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        setup.monitor._proc = None

        await setup.monitor.on_start()
        for task in setup.monitor._tasks:
            task.cancel()
        await asyncio.wait(setup.monitor._tasks, timeout=5)

        assert worker.actor_id in setup.monitor._last_seen
        assert setup.monitor.actor_id not in setup.monitor._last_seen

    async def test_a_failing_cpu_baseline_does_not_stop_the_start(self, setup: _Setup) -> None:
        class _Broken:
            def cpu_percent(self, interval: float | None = None) -> float:
                raise RuntimeError("no /proc")

        setup.monitor._proc = _Broken()

        await setup.monitor.on_start()
        for task in setup.monitor._tasks:
            task.cancel()
        await asyncio.wait(setup.monitor._tasks, timeout=5)

        assert len(setup.monitor._tasks) == 1


class TestLivenessFromMessages:
    async def test_any_message_counts_as_a_sign_of_life(self, setup: _Setup) -> None:
        await setup.monitor.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="w1"))

        assert "w1" in setup.monitor._last_seen

    async def test_its_own_messages_do_not(self, setup: _Setup) -> None:
        own = Message(type=MessageType.HEARTBEAT, sender_id=setup.monitor.actor_id)

        await setup.monitor.handle_message(own)

        assert setup.monitor._last_seen == {}

    async def test_a_message_from_an_alerted_actor_clears_the_alert(self, setup: _Setup) -> None:
        setup.monitor._alert_state["w1"] = True

        await setup.monitor.handle_message(Message(type=MessageType.RESULT, sender_id="w1"))

        assert setup.monitor._alert_state["w1"] is False

    async def test_an_error_event_is_recorded(self, setup: _Setup) -> None:
        event = _error_event()

        await setup.monitor.handle_message(
            Message(type=MessageType.TASK, sender_id="w1", payload=event)
        )

        assert setup.monitor._error_registry[event["actor_id"]] is event

    async def test_a_plain_task_is_not_an_error_event(self, setup: _Setup) -> None:
        await setup.monitor.handle_message(
            Message(type=MessageType.TASK, sender_id="w1", payload={"text": "hi"})
        )

        assert setup.monitor._error_registry == {}


class TestErrorEvents:
    async def test_a_warning_is_published_but_not_sent_to_the_user(self, setup: _Setup) -> None:
        event = _error_event()

        await setup.monitor._handle_error_event(event)

        topic, payload = setup.broker.published[-1]
        assert topic == f"agents/{event['actor_id']}/alert"
        assert '"[loop] boom"' in payload
        assert setup.notifications() == []

    async def test_a_fatal_error_reaches_the_user_as_critical(self, setup: _Setup) -> None:
        await setup.monitor._handle_error_event(_error_event(fatal=True))

        (note,) = setup.notifications()
        assert note["severity"] == "critical"
        assert "cannot run" in note["message"]
        assert note["agent_name"] == "sensor"

    async def test_a_critical_error_reaches_the_user(self, setup: _Setup) -> None:
        await setup.monitor._handle_error_event(_error_event(severity="critical", consecutive=4))

        (note,) = setup.notifications()
        assert "(4x)" in note["message"]

    async def test_a_degraded_agent_reaches_the_user(self, setup: _Setup) -> None:
        await setup.monitor._handle_error_event(_error_event(degraded=True))

        assert len(setup.notifications()) == 1

    async def test_the_name_falls_back_to_the_short_id(self, setup: _Setup) -> None:
        event = _error_event(fatal=True)
        del event["name"]

        await setup.monitor._handle_error_event(event)

        (note,) = setup.notifications()
        assert note["agent_name"] == event["actor_id"][:8]


class TestNotificationCooldown:
    async def test_a_repeat_inside_the_cooldown_is_suppressed(self, setup: _Setup) -> None:
        await setup.monitor._notify_main("w1", "sensor", "first", severity="critical")
        await setup.monitor._notify_main("w1", "sensor", "second", severity="critical")

        assert [n["message"] for n in setup.notifications()] == ["first"]

    async def test_a_repeat_after_the_cooldown_goes_through(self, setup: _Setup) -> None:
        setup.monitor._last_notified["w1"] = time.time() - _NOTIFY_COOLDOWN - 1

        await setup.monitor._notify_main("w1", "sensor", "again", severity="critical")

        assert len(setup.notifications()) == 1

    async def test_recovery_news_is_never_held_back(self, setup: _Setup) -> None:
        await setup.monitor._notify_main("w1", "sensor", "broken", severity="critical")
        await setup.monitor._notify_main("w1", "sensor", "recovered", severity="info")

        assert [n["message"] for n in setup.notifications()] == ["broken", "recovered"]

    async def test_other_actors_have_their_own_cooldown(self, setup: _Setup) -> None:
        await setup.monitor._notify_main("w1", "one", "m1", severity="critical")
        await setup.monitor._notify_main("w2", "two", "m2", severity="critical")

        assert len(setup.notifications()) == 2

    async def test_nothing_is_sent_without_an_orchestrator(self, setup: _Setup) -> None:
        setup.has_main = False

        await setup.monitor._notify_main("w1", "sensor", "lost", severity="critical")

        assert setup.notifications() == []

    async def test_nothing_is_sent_without_a_registry(self, tmp_path: Path) -> None:
        monitor = MonitorActor(persistence_dir=str(tmp_path))

        await monitor._notify_main("w1", "sensor", "lost", severity="critical")

        assert "w1" in monitor._last_notified

    async def test_a_failed_send_is_logged_not_raised(
        self, setup: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _refuse(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("mailbox gone")

        monkeypatch.setattr(setup.monitor, "send", _refuse)

        await setup.monitor._notify_main("w1", "sensor", "lost", severity="critical")


class TestHeartbeatChecks:
    @staticmethod
    def _silence(actor: Actor, seconds: float) -> None:
        past = time.time() - seconds
        actor.state = ActorState.RUNNING
        actor.metrics.start_time = past
        actor.metrics.last_heartbeat = past

    async def test_an_actor_seen_for_the_first_time_is_only_recorded(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        self._silence(worker, 1000)

        await setup.monitor._check_all_actors()

        assert worker.actor_id in setup.monitor._last_seen
        assert setup.broker.published == []

    async def test_a_silent_running_actor_raises_one_alert(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        self._silence(worker, 1000)
        setup.monitor._last_seen[worker.actor_id] = time.time() - 1000

        await setup.monitor._check_all_actors()
        await setup.monitor._check_all_actors()

        assert setup.broker.topics() == [f"agents/{worker.actor_id}/alert"]
        assert '"severity": "critical"' in setup.broker.published[0][1]
        (note,) = setup.notifications()
        assert "unresponsive" in note["message"]

    async def test_a_short_silence_is_only_a_warning(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        setup.monitor.heartbeat_timeout = 10
        self._silence(worker, 30)
        setup.monitor._last_seen[worker.actor_id] = time.time() - 30

        await setup.monitor._check_all_actors()

        assert '"severity": "warning"' in setup.broker.published[0][1]

    async def test_infrastructure_agents_are_not_reported_to_the_user(self, setup: _Setup) -> None:
        worker = await setup.worker("installer")
        self._silence(worker, 1000)
        setup.monitor._last_seen[worker.actor_id] = time.time() - 1000

        await setup.monitor._check_all_actors()

        assert len(setup.broker.published) == 1
        assert setup.notifications() == []

    async def test_a_stopped_actor_is_not_alerted(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        self._silence(worker, 1000)
        worker.state = ActorState.STOPPED
        setup.monitor._last_seen[worker.actor_id] = time.time() - 1000

        await setup.monitor._check_all_actors()

        assert setup.broker.published == []

    async def test_a_fresh_heartbeat_clears_the_alert(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        self._silence(worker, 1000)
        worker.metrics.last_heartbeat = time.time()
        setup.monitor._last_seen[worker.actor_id] = time.time() - 1000
        setup.monitor._alert_state[worker.actor_id] = True

        await setup.monitor._check_all_actors()

        assert setup.monitor._alert_state[worker.actor_id] is False

    async def test_a_recently_started_actor_counts_as_seen(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        worker.state = ActorState.RUNNING
        worker.metrics.start_time = time.time()
        worker.metrics.last_heartbeat = 0
        setup.monitor._last_seen[worker.actor_id] = time.time() - 1000

        await setup.monitor._check_all_actors()

        assert setup.broker.published == []

    async def test_without_a_registry_there_is_nothing_to_check(self, tmp_path: Path) -> None:
        monitor = MonitorActor(persistence_dir=str(tmp_path))

        await monitor._check_all_actors()
        await monitor._ping_all_actors()
        await monitor._publish_system_health()

        assert monitor._last_seen == {}


class TestPing:
    async def test_every_other_actor_is_asked_for_its_status(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")

        await setup.monitor._ping_all_actors()

        msg = worker._mailbox.get_nowait()
        assert msg.type == MessageType.STATUS_REQUEST
        assert setup.monitor._mailbox.empty()

    async def test_an_unreachable_actor_does_not_stop_the_round(
        self, setup: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await setup.worker("sensor")
        calls: list[str] = []

        async def _refuse(target_id: str, *_args: Any) -> bool:
            calls.append(target_id)
            raise RuntimeError("full")

        monkeypatch.setattr(setup.monitor, "send", _refuse)

        await setup.monitor._ping_all_actors()

        assert len(calls) == 2  # the orchestrator and the worker


class TestRecovery:
    async def test_an_agent_back_to_zero_errors_is_announced_once(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        setup.monitor._error_registry[worker.actor_id] = {"name": "sensor"}

        await setup.monitor._check_error_registry()
        await setup.monitor._check_error_registry()

        (note,) = setup.notifications()
        assert note["severity"] == "info"
        assert "recovered" in note["message"]
        assert setup.monitor._error_registry == {}

    async def test_an_agent_still_failing_stays_degraded(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")
        worker._consecutive_errors = 3
        setup.monitor._error_registry[worker.actor_id] = {"name": "sensor"}

        await setup.monitor._check_error_registry()

        assert worker.actor_id in setup.monitor._error_registry
        assert setup.notifications() == []

    async def test_an_unknown_actor_is_left_for_the_departure_sweep(self, setup: _Setup) -> None:
        setup.monitor._error_registry["gone"] = {}

        await setup.monitor._check_error_registry()

        assert "gone" in setup.monitor._error_registry


class TestSystemHealth:
    async def test_the_frame_counts_actors_by_state(self, setup: _Setup) -> None:
        running = await setup.worker("running")
        running.state = ActorState.RUNNING
        failed = await setup.worker("failed")
        failed.state = ActorState.FAILED
        failed._consecutive_errors = 2
        setup.monitor.state = ActorState.STOPPED
        setup.monitor._error_registry[failed.actor_id] = {}

        await setup.monitor._publish_system_health()

        topic, payload = setup.broker.published[-1]
        assert topic == "system/health"
        health = json.loads(payload)
        assert health["total_actors"] == 4
        assert health["running"] == 1
        assert health["failed"] == 1
        assert health["stopped"] == 1
        assert health["degraded"] == 1
        by_name = {a["name"]: a for a in health["actors"]}
        assert by_name["failed"]["consecutive_errors"] == 2

    async def test_host_stats_are_skipped_without_a_process_handle(self, setup: _Setup) -> None:
        setup.monitor._proc = None

        await setup.monitor._publish_host_stats()

        assert setup.broker.published == []

    async def test_a_host_stats_failure_publishes_nothing(self, setup: _Setup) -> None:
        class _Broken:
            def cpu_percent(self, interval: float | None = None) -> float:
                raise RuntimeError("no /proc")

        setup.monitor._proc = _Broken()

        await setup.monitor._publish_host_stats()

        assert setup.broker.published == []


class TestFindActor:
    async def test_it_finds_a_registered_actor_by_id(self, setup: _Setup) -> None:
        worker = await setup.worker("sensor")

        assert setup.monitor._find_actor(worker.actor_id) is worker

    async def test_an_unknown_id_is_none(self, setup: _Setup) -> None:
        assert setup.monitor._find_actor("nope") is None

    def test_without_a_registry_it_is_none(self, tmp_path: Path) -> None:
        assert MonitorActor(persistence_dir=str(tmp_path))._find_actor("x") is None


class TestMonitorLoop:
    async def test_one_failing_step_does_not_end_the_loop(
        self, setup: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setup.monitor.check_interval = 0
        rounds: list[int] = []

        async def _flaky() -> None:
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("transient")
            if len(rounds) == 3:
                setup.monitor.state = ActorState.STOPPED

        monkeypatch.setattr(setup.monitor, "_ping_all_actors", _flaky)

        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        task = asyncio.create_task(setup.monitor._monitor_loop())
        done, _ = await asyncio.wait({task}, timeout=5)

        assert task in done
        assert len(rounds) == 3

    async def test_cancellation_ends_the_loop_quietly(self, setup: _Setup) -> None:
        setup.monitor.check_interval = 60
        task = asyncio.create_task(setup.monitor._monitor_loop())
        await asyncio.sleep(0)

        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)

        assert task in done
        assert not task.cancelled()
