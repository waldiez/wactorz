"""What an agent says about itself every heartbeat.

Agents share one process, and so one CPU reading. The monitor publishes it once,
on `system/host`; measured per actor it was that same number under every
agent's name, so a heartbeat carries none.
"""

from typing import Any

from wactorz.core.actor import Actor


class _Agent(Actor):
    async def handle_message(self, message: Any) -> None:  # pragma: no cover - never sent one
        return None


def test_a_heartbeat_carries_no_cpu_figure(tmp_path: Any) -> None:
    beat = _Agent(name="worker", persistence_dir=str(tmp_path))._build_heartbeat()

    assert "cpu" not in beat
    assert {"actor_id", "name", "state", "memory_mb", "task", "timestamp"} <= beat.keys()


def test_an_actor_holds_no_process_handle_of_its_own(tmp_path: Any) -> None:
    agent = _Agent(name="worker", persistence_dir=str(tmp_path))

    assert not hasattr(agent, "_proc")
