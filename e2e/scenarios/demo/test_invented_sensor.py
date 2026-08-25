"""The same story as the nursery one, on a house nobody had to own.

`test_nursery_fan.py` asks a model to watch a nursery temperature and turn a fan
on, and names no entity on purpose - naming one would assert what the model
said. That is right for what it tests, and it is exactly why it cannot run on a
stranger's instance: with no such sensor and no such fan there is nothing for
the model to find, and the honest answer it gives back reads as a product fault.

So this story brings its own sensor. Home Assistant will hold a state for an
entity no integration owns, which means the scenario can invent one, drive it,
and forget it again - no device, no helper anyone must create first, nothing of
the reader's touched. See `harness.homeassistant` for why it invents a *reading*
and never a switch.

**The assertions are fingerprints, not phrases.** A reading is set to a value no
model would produce by chance, and the check is that the number comes back. That
cannot be satisfied by a plausible sentence: either something read Home
Assistant or it did not. It is the one assertion here about a model's words that
is safe to make, and it is why this file can check a real answer where the
nursery story can only check that an answer arrived.

**The file needs an instance; only two of its steps need a real model.** Setting
the automation up and seeing its card runs under the fake provider like the
nursery story does, which is what lets `rehearse` be used to pace this one and
what keeps the structure from going stale between recordings. The two
fingerprint checks are the ones a script cannot honestly answer, so those carry
`requires_llm("real")` and skip until `make e2e-demo`.

⚠ **It writes to the instance whenever it runs, not only in a demo.** A `test`
run with `HA_URL` set creates the sensor, drives it, and deletes it. Nothing of
anyone's is touched and nothing survives the module - but it is a write to a
real house during an ordinary regression run, which the nursery story never
makes, and that is worth knowing before pointing this suite at somewhere you
care about.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import profiles
import pytest
from harness import backend, browser, homeassistant, waiting

pytestmark = pytest.mark.requires_ha

#: Who a person setting up an automation is talking to.
ASSISTANT = "main"

#: The reading this story invents, and the two values it is set to. Both are
#: odd enough that a model repeating one has read it rather than guessed it.
SENSOR_SLUG = "porch_temperature"
COLD = 4.3
HOT = 41.7

#: Said back when a plan is waiting to be built. Product template, not model
#: prose, so matching on it asserts nothing about what a model happened to say.
APPROVAL_MARKER = "To proceed:"
APPROVE = "yes"

#: A planner is machinery, not the automation - see the nursery story, where
#: counting it as the thing built made the scenario pass while nothing was.
MACHINERY_PREFIX = "planner-"


@pytest.fixture(scope="module", name="reading")
def reading_fixture() -> Iterator[homeassistant.Reading]:
    """The invented sensor, gone again by the end of the module.

    Created before the backend so that the system has never seen the instance
    without it, which is what a real sensor would look like.
    """
    with homeassistant.invented_reading(SENSOR_SLUG, COLD) as sensor:
        yield sensor


@pytest.fixture(scope="module", name="story")
def story_fixture(
    reading: homeassistant.Reading,
    profile: profiles.Profile,
    run_out: Path,
    logs_dir: Path,
    script: str,
) -> Iterator[backend.Backend]:
    """A system of this story's own, for the reasons the nursery story gives."""
    instance = backend.start(
        state_dir=run_out / "invented-sensor",
        console_log=logs_dir / "invented-sensor.log",
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
    """One page across the story, so a recording is one take."""
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
    """Where the story writes down the agent it created, as the nursery one does."""
    return {}


def _created_agent(automation: dict[str, str]) -> str:
    name = automation.get("name")
    if not name:
        raise AssertionError(
            "the automation was never created, so there is nothing to talk to - "
            "the step that sets it up must have failed first"
        )
    return name


@pytest.mark.requires_llm("real")
def test_the_invented_sensor_is_visible_to_the_system(
    dashboard: browser.Dashboard, reading: homeassistant.Reading
) -> None:
    """Before anything is built: can this system see the sensor at all?

    First because everything after it depends on the answer, and because a
    failure here is a different fault entirely - the instance is unreachable or
    the integration is not reading it - and saying so in one step beats watching
    an automation get built on a sensor nobody can see.
    """
    assert reading.value == str(COLD), (
        f"the invented sensor should read {COLD} before anyone asks about it, "
        f"and reads {reading.value!r}"
    )

    before = len(dashboard.replies())
    dashboard.chat(f"What is {reading.entity_id} reading right now?", to=ASSISTANT, dwell="beat")
    reply = dashboard.wait_for_reply(after=before, dwell="readable")

    assert str(COLD) in reply, (
        f"asked for {reading.entity_id}, which Home Assistant is holding at {COLD}. "
        f"The answer never mentions that value, so nothing read the instance: {reply[:300]!r}"
    )


def test_an_automation_can_be_set_up_for_it(
    dashboard: browser.Dashboard,
    story: backend.Backend,
    reading: homeassistant.Reading,
    automation: dict[str, str],
) -> None:
    """Ask for it in words, say yes to the plan, and the automation exists."""
    before_agents = {a["name"] for a in story.rest.agents()}
    before_replies = len(dashboard.replies())

    dashboard.chat(
        f"Watch {reading.entity_id} and tell me when it goes above 30 degrees.",
        to=ASSISTANT,
        dwell="beat",
    )
    reply = dashboard.wait_for_reply(after=before_replies, dwell="readable")
    assert reply.strip(), "the assistant answered the request with nothing"

    if APPROVAL_MARKER in reply:
        # Answered rather than bypassed: `main` takes a `pipeline!` prefix that
        # skips approval, and using it would hide the one step where the person
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


@pytest.mark.requires_llm("real")
def test_the_agent_reads_the_sensor_after_it_moves(
    dashboard: browser.Dashboard,
    reading: homeassistant.Reading,
    automation: dict[str, str],
) -> None:
    """Drive the sensor, then ask the thing that was built what it sees.

    The second fingerprint is what makes this a live read rather than a
    remembered one: the agent was created while the sensor said something else,
    so a value it repeats now had to come from the instance.
    """
    created = _created_agent(automation)
    reading.set(HOT)
    assert reading.value == str(HOT), "the instance did not take the new reading"

    before = len(dashboard.replies())
    dashboard.chat("What is that sensor reading now?", to=created, dwell="beat")
    reply = dashboard.wait_for_reply(after=before, dwell="settle")

    assert str(HOT) in reply, (
        f"{created!r} was built to watch {reading.entity_id}, which now reads {HOT}. "
        f"Its answer does not contain that value, so it is not reading the sensor "
        f"it was built for: {reply[:300]!r}"
    )
