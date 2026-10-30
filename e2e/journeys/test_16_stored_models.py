"""Stored models: the versions kept, the one served and the one tried, moved from the page.

Two versions of a model are stored, and the first made active, from the command
line, as an operator does. The Models view marks the one served. The second is
tried beside it and then promoted from the page, each with a reason; a second
browser, signed in from a link to the model, opens on it and sees each move as
main publishes it, and the history says who moved what, and why. The first is
then tried again and its trial cleared, with a reason the history keeps too.

It starts from what journey 15 left; it touches no agent.
"""

import subprocess
import sys

from harness import browser, waiting
from harness.run import Run

MODEL = "e2e-sensor"

#: Long enough for main to find a move and publish it (it looks every few
#: seconds), and for the page to read the model again.
SEEN_WITHIN_S = 30.0


def _artifacts(run: Run, *args: str) -> None:
    """Run the artifacts CLI on the install's own store, as an operator on main would."""
    command, *rest = args
    subprocess.run(
        [
            sys.executable,
            "-m",
            "wactorz.artifacts_cli",
            command,
            "--state-dir",
            str(run.state),
            *rest,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _steps_become(page: browser.Dashboard, wanted: list[str], what: str) -> None:
    waiting.until(
        lambda: page.model_steps() == wanted, what=what, timeout=SEEN_WITHIN_S, interval=0.5
    )


def test_the_stored_versions_are_shown_with_the_one_served(
    dashboard: browser.Dashboard, run: Run
) -> None:
    for version in (1, 2):
        stored = run.root / f"{MODEL}-{version}.bin"
        stored.write_bytes(bytes([version]) * 64)
        _artifacts(
            run, "add", "--format", "raw", "--meta", f"note=version {version}", str(stored), MODEL
        )
    _artifacts(run, "activate", f"{MODEL}:1")

    dashboard.open_address(f"#/models/{MODEL}")

    _steps_become(dashboard, ["v1, active", "v2"], "the model shown, v1 marked served")
    assert dashboard.model_offers() == []
    dashboard.assert_accessible("a stored model, the version served marked")


def test_a_version_tried_and_promoted_here_is_seen_in_another_browser(
    dashboard: browser.Dashboard, visitor: browser.Dashboard, run: Run
) -> None:
    # A link to the model, followed in a browser that has not signed in: the
    # sign-in keeps the view it named.
    visitor.sign_in_at(f"#/models/{MODEL}", run.api_key)
    assert visitor.address == f"#/models/{MODEL}"
    _steps_become(visitor, ["v1, active", "v2"], "the second browser to show the model")
    # Opened again: between journeys the guard reads every chat thread, which leaves the chat open.
    dashboard.open_address(f"#/models/{MODEL}")
    _steps_become(dashboard, ["v1, active", "v2"], "this browser to show the model")

    dashboard.press_on_model("Try v2 as shadow")
    dashboard.confirm_with_reason("quieter at night")

    _steps_become(visitor, ["v1, active", "v2, shadow"], "the second browser to see v2 tried")
    assert dashboard.model_offers() == ["Promote v2", "Clear shadow"]

    dashboard.press_on_model("Promote v2")
    dashboard.confirm_with_reason("fewer false alarms")

    _steps_become(visitor, ["v1, previous", "v2, active"], "the second browser to see v2 served")
    _steps_become(dashboard, ["v1, previous", "v2, active"], "this browser to see v2 served")
    assert dashboard.model_offers() == ["Roll back to v1"]
    moves = dashboard.model_moves()
    assert any(
        m.startswith("active → v2 (was v1), by dashboard") and "“fewer false alarms”" in m
        for m in moves
    ), moves
    assert any(
        m.startswith("shadow → v2, by dashboard") and "“quieter at night”" in m for m in moves
    ), moves
    dashboard.show("overview")


def test_a_trial_cleared_here_keeps_its_reason(dashboard: browser.Dashboard) -> None:
    dashboard.open_address(f"#/models/{MODEL}")
    _steps_become(dashboard, ["v1, previous", "v2, active"], "the model as the promotion left it")

    dashboard.press_on_model("Try v1 as shadow")
    dashboard.confirm_with_reason("one more night")
    _steps_become(dashboard, ["v1, shadow", "v2, active"], "v1 tried again")

    dashboard.press_on_model("Clear shadow")
    dashboard.confirm_with_reason("v2 holds up")

    _steps_become(dashboard, ["v1, previous", "v2, active"], "the trial of v1 ended")
    # The pointers show the clear at once; the history has it once main records it.
    waiting.until(
        lambda: any(
            m.startswith("shadow cleared (was v1), by dashboard") and "“v2 holds up”" in m
            for m in dashboard.model_moves()
        ),
        what="the clear in the history, with its reason",
        timeout=SEEN_WITHIN_S,
        interval=0.5,
    )
    dashboard.show("overview")
