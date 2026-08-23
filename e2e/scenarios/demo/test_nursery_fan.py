"""When the nursery goes above 26 degrees, turn the fan on.

The shortest story that is recognisably the product: a person asks for an
automation in plain language, an agent appears to do it, and the dashboard shows
it happening. Set up in chat, watched on the overview, with the card, the reply
and the feed all on screen.

Two rules shape what this file may assert, and the first take against a real
model broke both of them.

**It names no agent.** An earlier version waited for one called `nursery-fan`,
which is the name the fake provider's script happens to invent. A real model
asked to set up an automation creates an agent and calls it whatever it likes,
so that assertion was really an assertion about what the model *said* - the one
thing the README says cannot hold under both providers. What holds is that an
agent exists which did not exist a moment ago. The story discovers its name and
uses it from then on.

**It answers the plan.** A request to watch a sensor and act on it is classified
`PIPELINE`, and main's answer to that is a *proposal*: a planner drafts the
agents, main prints them and asks to be told to go ahead. A story that asks and
walks away gets a plan and no automation - which is what happened, and the
scenario passed anyway, because the planner main spawned to draw up the plan was
itself a new agent. The story now says yes, and does not count machinery as the
thing it asked for.

**It addresses `main` on purpose.** The composer's default is not something a
scenario may lean on: see `.local/plans/chat-target-default.md` for the bug where
it settles on `catalog`, and a demo that quietly asked the catalogue agent to
build an automation got a catalogue listing back. The person in this story is
talking to the assistant, so the story says so.

Paced with `dwell`, which the test profile ignores entirely. What the agent said
is asserted only in the one test marked `requires_llm("real")`, which `test`
skips.
"""

from collections.abc import Iterator
from pathlib import Path

import profiles
import pytest
from harness import backend, browser, waiting

#: Who a person setting up an automation is talking to.
ASSISTANT = "main"

ASK = "Watch the nursery temperature and turn the fan on when it passes 26 degrees."
FOLLOW_UP = "How warm is the nursery right now?"

#: Said back when a plan is waiting to be built. Product template, not model
#: prose - `main.planning` writes these two lines verbatim - so matching on it is
#: not an assertion about what a model happened to say.
APPROVAL_MARKER = "To proceed:"

#: One of the words `_APPROVE_PHRASES` accepts.
APPROVE = "yes"

#: Planners are machinery, not the automation. A request like this one is
#: classified PIPELINE, and main answers it by spawning a planner to draw up the
#: plan - so the first new agent to appear is the planner, every time. Counting
#: it as "the automation" made this scenario pass while nothing was built, and
#: then sent the rest of the story to the planner instead of to the thing the
#: story is about.
MACHINERY_PREFIX = "planner-"


@pytest.fixture(scope="module", name="story")
def story_fixture(
    profile: profiles.Profile, run_out: Path, logs_dir: Path, script: str
) -> Iterator[backend.Backend]:
    """A system of this story's own, from empty to the end of the demo.

    Its own rather than the shared one, and the reason is collection order:
    `demo/` sorts before `test_a01`, so a demo sharing the session backend hands
    the regression core a system with this story's agents already in it. Nothing
    in the core asserts a clean baseline today, so it was harmless - but that is
    the kind of coupling nobody sees until someone writes the scenario that does,
    and then it reads as a product bug.

    It also means the story starts from nothing, which is what a recording of it
    should show.
    """
    instance = backend.start(
        state_dir=run_out / "nursery",
        console_log=logs_dir / "nursery.log",
        llm=profile.llm,
        script=script,
    )
    try:
        backend.wait_until_settled(instance)
        yield instance
    finally:
        instance.kill()


@pytest.fixture(scope="module", name="dashboard")
def dashboard_fixture(
    story: backend.Backend,
    profile: profiles.Profile,
    videos_dir: Path | None,
    playwright_driver: browser.Playwright,
) -> Iterator[browser.Dashboard]:
    """The story's own dashboard, held open across the whole demo.

    Shadows the session-wide `dashboard` fixture deliberately: every scenario
    here drives this system, and one page across the story is also what makes a
    recording one continuous take rather than four browser launches.
    """
    with browser.browser_page(
        playwright_driver,
        base_url=story.url,
        headless=profile.headless,
        video_dir=videos_dir,
        dwell_seconds=profile.dwell,
        size=profile.size,
    ) as page:
        yield page.open()


@pytest.fixture(scope="module", name="automation")
def automation_fixture() -> dict[str, str]:
    """Where the story writes down the name of the agent it created.

    Shared mutable state between tests, which is ordinarily a smell and is the
    point here: the steps are a story in order, each one acting on what the last
    one produced. A test that runs before the name is known says so by name
    rather than failing on a `KeyError`.
    """
    return {}


def _created_agent(automation: dict[str, str]) -> str:
    name = automation.get("name")
    if not name:
        raise AssertionError(
            "the automation was never created, so there is nothing to talk to - "
            "the step that sets it up must have failed first"
        )
    return name


def test_the_automation_can_be_set_up_in_conversation(
    dashboard: browser.Dashboard, story: backend.Backend, automation: dict[str, str]
) -> None:
    """Ask for it in words, say yes to the plan, and the automation exists.

    The second step is the one this scenario was missing. Asked to watch a sensor
    and act on it, main does not build anything on the spot - it draws up a plan
    and asks to be told to go ahead. Nobody answered, so nothing was ever built,
    and the story quietly ended at a proposal.
    """
    before_agents = {a["name"] for a in story.rest.agents()}
    before_replies = len(dashboard.replies())

    dashboard.chat(ASK, to=ASSISTANT, dwell="beat")
    reply = dashboard.wait_for_reply(after=before_replies, dwell="readable")
    assert reply.strip(), "the assistant answered the request with nothing"

    if APPROVAL_MARKER in reply:
        # A plan, waiting to be built. Answered rather than bypassed: `main`
        # takes a `pipeline!` prefix that skips approval entirely, and using it
        # would keep the recording tidy by hiding the one step where the person
        # is actually asked to decide something.
        approved_from = len(dashboard.replies())
        dashboard.chat(APPROVE, to=ASSISTANT, dwell="beat")
        dashboard.wait_for_reply(after=approved_from, dwell="readable")

    def appeared() -> str:
        new = {
            name
            for name in {a["name"] for a in story.rest.agents()} - before_agents
            if not name.startswith(MACHINERY_PREFIX)
        }
        return sorted(new)[0] if new else ""

    created = waiting.until(
        appeared,
        what="an agent to be created from the conversation",
        timeout=180.0,
        interval=0.5,
    )
    automation["name"] = created

    waiting.becomes_and_stays(
        lambda: story.rest.state_of(created) == "running",
        what=f"{created!r} to be running and stay running",
        timeout=90.0,
        window=3.0,
        interval=0.25,
    )


def test_the_new_agent_appears_on_the_dashboard(
    dashboard: browser.Dashboard, automation: dict[str, str]
) -> None:
    """The card is the moment the story lands, so it gets the longest dwell."""
    created = _created_agent(automation)
    dashboard.show("overview", dwell="beat")
    dashboard.wait_for_card(created, dwell="settle")


def test_the_conversation_can_be_continued_with_the_new_agent(
    dashboard: browser.Dashboard, automation: dict[str, str]
) -> None:
    """Talk to the thing that was just made, and it answers.

    The claim is that the agent created a moment ago is a real correspondent -
    reachable, addressed, and answering - not a card with nothing behind it.

    Says nothing about spend. `a05` owns that claim and makes it against `main`,
    where it holds; asserting it for a spawned agent here would be asserting a
    behaviour this scenario cannot vouch for either way.
    """
    created = _created_agent(automation)
    before = len(dashboard.replies())

    dashboard.chat(FOLLOW_UP, to=created, dwell="beat")
    reply = dashboard.wait_for_reply(after=before, dwell="settle")

    assert reply.strip(), f"{created!r} answered with nothing"
    assert dashboard.target() == created, (
        f"the conversation moved to {dashboard.target()!r} mid-story"
    )


def test_the_feed_shows_it_happening(dashboard: browser.Dashboard) -> None:
    """The last shot: the activity feed, with the story in it."""
    dashboard.show("feed", dwell="settle")
    assert dashboard.renders("feed"), "the activity feed did not render"


@pytest.mark.requires_llm("real")
def test_the_agent_talks_about_the_nursery(
    dashboard: browser.Dashboard, automation: dict[str, str]
) -> None:
    """It answers about the thing it was made for, in its own words.

    The one assertion here that is about content, which is why it is behind the
    marker: under the fake provider the reply is scripted, so asserting it would
    be asserting the script. Skipped under `test`, which means it can go stale
    without anyone noticing - re-run the demo profile before sharing a take.
    """
    created = _created_agent(automation)
    before = len(dashboard.replies())
    dashboard.chat("Is the fan on?", to=created, dwell="beat")
    reply = dashboard.wait_for_reply(after=before, dwell="settle").lower()

    assert "fan" in reply or "nursery" in reply, (
        f"asked whether the fan was on, {created!r} said something about neither "
        f"the fan nor the nursery: {reply[:200]!r}"
    )
