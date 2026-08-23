"""The pre-tag walkthrough: everything a person does in a browser, in order.

This is the manual checklist somebody used to work through before a tag — open
it, look at each page, say something, make an agent, check it is there, put it
away, wipe it, confirm it came back empty. Written as one ordered scenario
because that is how it was done, and because each step's precondition is the
step before it.

It runs on its own backend. Every other scenario shares one and asserts about
part of it; this one owns the whole lifetime, from an empty system to a wiped
one, and would otherwise take the shared backend's agents with it.

Nothing here is deep. Each step is the shallowest possible check that the thing
a person would do at that point actually works — depth is what the regression
core is for, and duplicating it here would just make this slow and this is the
part that has to stay readable.
"""

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import profiles
import pytest
from harness import backend, browser, waiting

AGENT = "weather-agent"


@pytest.fixture(scope="module", name="walkthrough")
def walkthrough_fixture(
    profile: profiles.Profile, run_out: Path, logs_dir: Path, script: str
) -> Iterator[backend.Backend]:
    """One backend for the whole walkthrough, in the order it is written.

    Module-scoped, so the steps below share the system they are walking through —
    which is what makes them steps rather than eleven unrelated startups.
    """
    instance = backend.start(
        state_dir=run_out / "walkthrough",
        console_log=logs_dir / "walkthrough.log",
        llm=profile.llm,
        script=script,
    )
    try:
        backend.wait_until_settled(instance)
        yield instance
    finally:
        instance.kill()


@pytest.fixture(scope="module", name="page")
def page_fixture(
    walkthrough: backend.Backend,
    profile: profiles.Profile,
    videos_dir: Path | None,
    playwright_driver: browser.Playwright,
) -> Iterator[browser.Dashboard]:
    with browser.browser_page(
        playwright_driver,
        base_url=walkthrough.url,
        headless=profile.headless,
        video_dir=videos_dir,
        dwell_seconds=profile.dwell,
        size=profile.size,
    ) as dashboard:
        yield dashboard.open()


# ── 1. Arrive ───────────────────────────────────────────────────────────────


def test_the_dashboard_opens_on_a_working_system(page: browser.Dashboard) -> None:
    page.show("overview", dwell="readable")
    page.wait_for_card("main")


# ── 2. Look around ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("view", browser.VIEWS)
def test_every_page_can_be_visited(page: browser.Dashboard, view: str) -> None:
    """Click each nav item and find something on the other side of it."""
    page.show(view, dwell="beat")
    assert page.renders(view), f"the {view} page was empty after navigating to it"


# ── 3. Say something ────────────────────────────────────────────────────────


def test_a_message_gets_an_answer(page: browser.Dashboard) -> None:
    page.show("chat")
    before = len(page.replies())
    page.chat("hello", to="main", dwell="beat")
    assert page.wait_for_reply(after=before, dwell="readable").strip(), (
        "the assistant answered with nothing"
    )


# ── 4. Make an agent ────────────────────────────────────────────────────────


def test_an_agent_can_be_created_from_the_chat(
    page: browser.Dashboard, walkthrough: backend.Backend
) -> None:
    before = len(page.replies())
    page.chat("spawn a weather agent", to="main", dwell="beat")
    page.wait_for_reply(after=before, dwell="readable")
    waiting.until(
        lambda: walkthrough.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to be created and running",
        timeout=120.0,
        interval=0.5,
    )


def test_the_new_agent_is_visible_and_addressable(page: browser.Dashboard) -> None:
    """It is on the overview and in the composer - the two places a person looks."""
    page.show("overview", dwell="beat")
    page.wait_for_card(AGENT, dwell="readable")

    page.show("chat")
    assert AGENT in page.target_options(), (
        f"{AGENT!r} has a card but cannot be addressed: {page.target_options()}"
    )


# ── 5. Put it away ──────────────────────────────────────────────────────────


def test_the_agent_can_be_stopped_and_started_again(walkthrough: backend.Backend) -> None:
    walkthrough.rest.stop(AGENT)
    waiting.until(
        lambda: walkthrough.rest.state_of(AGENT) == "stopped",
        what=f"{AGENT!r} to stop",
        timeout=60.0,
        interval=0.25,
    )
    assert walkthrough.rest.command(AGENT, "start").ok, f"starting {AGENT!r} was refused"
    waiting.until(
        lambda: walkthrough.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to start again",
        timeout=90.0,
        interval=0.25,
    )


def test_the_agent_can_be_deleted(page: browser.Dashboard, walkthrough: backend.Backend) -> None:
    assert walkthrough.rest.delete(AGENT).ok, f"deleting {AGENT!r} was refused"
    page.show("overview")
    waiting.until(
        lambda: not page.has_card(AGENT),
        what=f"the card for {AGENT!r} to disappear",
        timeout=60.0,
        interval=0.25,
    )


# ── 6. Wipe it ──────────────────────────────────────────────────────────────


def test_a_reset_leaves_the_system_empty_and_usable(
    walkthrough: backend.Backend,
    own_app: Callable[..., backend.Backend],
    browse: Callable[[backend.Backend], browser.Dashboard],
) -> None:
    """The last thing on the checklist, and the one nobody wants to do by hand.

    Asserted as usable, not merely empty: a system that comes back with nothing
    in it and cannot be talked to has passed half of what a reset promises.
    """
    walkthrough.rest.chat("spawn a weather agent")
    waiting.until(
        lambda: walkthrough.rest.agent(AGENT) is not None,
        what=f"{AGENT!r} to exist before the wipe",
        timeout=120.0,
        interval=0.5,
    )
    walkthrough.interrupt()

    result = subprocess.run(
        [sys.executable, "-m", "wactorz.reset", "--all", "--state-dir", str(walkthrough.state_dir)],
        cwd=backend.REPO_ROOT,
        env=backend.environment(
            state_dir=walkthrough.state_dir,
            port=walkthrough.port,
            api_port=walkthrough.api_port,
            llm="fake",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"wactorz-reset failed:\n{result.stdout}\n{result.stderr}"

    restarted: backend.Backend = own_app(state_dir=walkthrough.state_dir)
    backend.wait_until_settled(restarted)
    assert restarted.rest.agent(AGENT) is None, f"{AGENT!r} survived the wipe"

    fresh: browser.Dashboard = browse(restarted)
    fresh.show("chat", dwell="readable")
    before = len(fresh.replies())
    fresh.chat("hello", to="main", dwell="beat")
    assert fresh.wait_for_reply(after=before, dwell="readable").strip(), (
        "the system came back empty but cannot be talked to"
    )
