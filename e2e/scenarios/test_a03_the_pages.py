"""Every page renders, and its data arrives.

Two halves, and the second is the one worth having. A view that swaps in an empty
container renders; a view whose data never arrives looks identical to a user for
the first second and different for every second after. So each view is asserted
on the content that only exists once the server has answered.
"""

import pytest
from harness import backend, browser


@pytest.mark.parametrize("view", browser.VIEWS)
def test_every_view_renders(dashboard: browser.Dashboard, view: str) -> None:
    """One test per view, so a failure names the page that broke."""
    dashboard.show(view, dwell="beat")
    assert dashboard.renders(view), f"the {view} view did not put its content on screen"


def test_the_overview_receives_its_agents(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    """The cards are the server's agents, not a placeholder.

    Compared against what the API reports rather than against a number: a
    hardcoded count asserts how many agents the application happened to start on
    the day this was written.
    """
    dashboard.show("overview", dwell="readable")
    dashboard.wait_for_card("main")
    expected = {a["name"] for a in app.rest.agents()}
    shown = dashboard.card_names()
    assert expected <= shown, f"the overview is missing cards for {sorted(expected - shown)}"


def test_the_chat_offers_the_agents_as_targets(dashboard: browser.Dashboard) -> None:
    """The composer knows who it can talk to, which needs the socket to have spoken."""
    dashboard.show("chat", dwell="beat")
    assert "main" in dashboard.target_options(), (
        f"the composer does not offer main as a target: {dashboard.target_options()}"
    )
