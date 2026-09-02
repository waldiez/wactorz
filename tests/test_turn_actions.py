"""User-facing summaries for agent lifecycle actions."""

from types import SimpleNamespace

from wactorz.agents.main.turn_actions import TurnActions
from wactorz.agents.mixins import SpawnPlaceholder


def test_reachy_install_summary_explains_what_is_happening() -> None:
    summary = TurnActions(spawned=(SpawnPlaceholder("reachy-mini"),)).summary("")

    assert summary == (
        "Preparing Reachy Mini — installing its robot and voice support; "
        "it will announce when ready"
    )


def test_reachy_started_summary_sets_the_connection_expectation() -> None:
    summary = TurnActions(spawned=(SimpleNamespace(name="reachy-mini"),)).summary("")

    assert summary == "Reachy Mini started — it will announce when the robot connection is ready"


def test_other_agents_keep_the_existing_summary() -> None:
    summary = TurnActions(spawned=(SpawnPlaceholder("chart-maker"),)).summary("")

    assert summary == "Installing packages for 'chart-maker' — will appear shortly"
