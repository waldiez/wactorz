"""User-facing summaries for agent lifecycle actions."""

from types import SimpleNamespace

from wactorz.agents.main.turn_actions import TurnActions
from wactorz.agents.mixins import SpawnPlaceholder


def test_an_agent_still_installing_is_said_to_appear_later() -> None:
    summary = TurnActions(spawned=(SpawnPlaceholder("chart-maker"),)).summary("")

    assert summary == "Installing packages for 'chart-maker' — will appear shortly"


def test_a_running_agent_is_said_to_survive_a_restart() -> None:
    summary = TurnActions(spawned=(SimpleNamespace(name="chart-maker"),)).summary("")

    assert summary == "Spawned 'chart-maker' — will auto-restore on restart"
