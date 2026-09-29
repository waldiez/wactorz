"""The CPU figure on the dashboard is a share of the whole machine.

psutil reports a process's CPU the way `top` does — per core, so a process
keeping two cores busy reads 200%. The dashboard draws it inside a meter that
runs to 100 and prints the number beside it, so the bar sat pegged at full while
the label disagreed with it, and neither told a reader how loaded the machine
actually was.

Dividing by the core count is the half that belongs here. The meter is left
alone: it was already right for a figure that cannot exceed 100.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import monitor_agent
from wactorz.agents.monitor_agent import MonitorActor


class Process:
    """A process using `cpu` percent, in psutil's per-core terms."""

    def __init__(self, cpu: float) -> None:
        self._cpu = cpu

    def cpu_percent(self, interval: float | None = None) -> float:
        return self._cpu

    def memory_info(self) -> Any:
        class _Mem:
            rss = 64 * 1024 * 1024

        return _Mem()


class RecordingMQTT:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload))

    async def disconnect(self) -> None:
        return None


async def host_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cpu: float, cores: int | None
) -> dict[str, Any]:
    """Publish one host-stats frame from a machine with `cores` cores."""
    monitor = MonitorActor(persistence_dir=str(tmp_path))
    broker = RecordingMQTT()
    monitor._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]
    monitor._proc = Process(cpu)  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(monitor_agent.psutil, "cpu_count", lambda: cores)

    await monitor._publish_host_stats()

    topic, payload = broker.published[-1]
    assert topic == "system/host"
    return json.loads(payload) if isinstance(payload, str) else payload


async def test_a_process_on_two_and_a_half_cores_reads_as_a_third_of_an_eight_core_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = await host_stats(tmp_path, monkeypatch, cpu=263.4, cores=8)

    assert stats["cpu"] == pytest.approx(32.925)


async def test_it_never_exceeds_the_meter_it_is_drawn_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every core busy is the most a process can use, and reads as 100%."""
    stats = await host_stats(tmp_path, monkeypatch, cpu=400.0, cores=4)

    assert stats["cpu"] == pytest.approx(100.0)


async def test_a_single_core_machine_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = await host_stats(tmp_path, monkeypatch, cpu=42.0, cores=1)

    assert stats["cpu"] == pytest.approx(42.0)


async def test_an_unknown_core_count_leaves_the_reading_as_psutil_gave_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cpu_count` answers None where it cannot tell. Dividing by nothing is not
    an option, and a figure that is too high beats no figure at all."""
    stats = await host_stats(tmp_path, monkeypatch, cpu=263.4, cores=None)

    assert stats["cpu"] == pytest.approx(263.4)


async def test_memory_is_reported_alongside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same frame carries memory, which the change must not disturb."""
    stats = await host_stats(tmp_path, monkeypatch, cpu=10.0, cores=4)

    assert stats["mem_used_mb"] == pytest.approx(64.0)
    assert stats["mem_total_mb"] > 0
