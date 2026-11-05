"""The wiring: two agents joined by a topic, and a topic nobody reads, flagged.

A package beside Wactorz brings four agents: one publishes pings, one reads
them, one shouts on a topic no agent reads, and one with a name and a topic
far too long for a box, which must not spill over its neighbours. The Wiring view draws the
first two joined by their topic and warns of the third; the warning shows its
topic alone, choosing a topic from the keyboard says who publishes and reads
it, and a box dragged stays where it was put, across a reload, until the
layout is reset. The page passes the browser's accessibility check as a graph and as
a list.

The package is real: a distribution's metadata in a folder on ``PYTHONPATH``,
registering its agents as entry points, named in ``WACTORZ_EXTENSIONS``.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import backend, browser, guard, waiting
from playwright.sync_api import Locator

DISTRIBUTION = "acme-wiring"

MODULE = """
import wactorz


@wactorz.agent(name="e2e-ping", subscribes="custom/e2e/tick", publishes="e2e/pings")
async def ping(tick: dict) -> dict:
    return {"ping": tick.get("n", 0)}


@wactorz.agent(name="e2e-pong", subscribes="e2e/pings")
async def pong(ping: dict) -> None:
    return None


@wactorz.agent(name="e2e-shout", subscribes="custom/e2e/tick", publishes="e2e/nobody")
async def shout(tick: dict) -> dict:
    return {"shout": True}


@wactorz.agent(
    name="e2e-a-reader-whose-name-is-far-too-long-for-any-box",
    subscribes="e2e/pings/and/a/topic/whose/name/is/far/too/long/for/its/box/as/well",
)
async def long_reader(ping: dict) -> None:
    return None
"""

ENTRY_POINTS = """[wactorz.agents]
e2e-ping = acme_wiring:ping
e2e-pong = acme_wiring:pong
e2e-shout = acme_wiring:shout
e2e-long = acme_wiring:long_reader
"""

#: Long enough for the agents to start and announce themselves, and the page to read the wiring.
SEEN_WITHIN_S = 60.0


@pytest.fixture(scope="module", name="site")
def site_fixture(app: backend.Backend) -> Path:
    assert app.run is not None
    site = app.run.root / "wiring-site"
    info = site / "acme_wiring-1.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (site / "acme_wiring.py").write_text(MODULE)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(ENTRY_POINTS)
    return site


@pytest.fixture(scope="module", name="installed")
def installed_fixture(app: backend.Backend, site: Path) -> Iterator[None]:
    app.restart(extra={"PYTHONPATH": str(site), "WACTORZ_EXTENSIONS": DISTRIBUTION})
    yield
    # The journeys after this one see the install as it was.
    app.restart()


def _signed_in_again(dashboard: browser.Dashboard, unexpected: guard.Guard) -> None:
    # While there is no server the page asks for it and is refused, which the
    # browser reports on its console.
    unexpected.allow_on_the_page(r"ERR_CONNECTION_REFUSED")
    waiting.until(
        lambda: not dashboard.at_sign_in and dashboard.connection() == "● live",
        what="the page to be fed again",
        timeout=60.0,
    )


def _view(dashboard: browser.Dashboard) -> Locator:
    return dashboard.page.get_by_test_id("view-wiring")


def _node(dashboard: browser.Dashboard, name: str) -> Locator:
    """The box whose name begins with ``name`` and ends there or at its next line, after a comma.

    Not a part of a name: "Topic e2e/pings" would also find a topic under it.
    """
    # A box's lines are blocks, which a browser reads with a space around them. A slash is
    # escaped too: Playwright writes the pattern into its selector between slashes.
    escaped = re.escape(name).replace("/", "\\/")
    return _view(dashboard).get_by_role("button", name=re.compile(rf"^{escaped}\s*(,|$)"))


def _opened(dashboard: browser.Dashboard) -> None:
    """The Wiring view as a graph, once it shows the package's agents."""
    dashboard.open_address("#/wiring")
    # A change of address within the page keeps the view as it was left: as a list, maybe.
    as_graph = _view(dashboard).get_by_role("button", name="View as graph")
    if as_graph.count():
        as_graph.click()
    waiting.until(
        lambda: _node(dashboard, "Agent e2e-pong").count() == 1,
        what="the wiring to show the package's agents",
        timeout=SEEN_WITHIN_S,
        interval=1.0,
    )


def _overlapping(dashboard: browser.Dashboard) -> list[str]:
    """Each pair of boxes drawn over each other, and each box taller than the layout made it."""
    # Read in one step inside the page: the view may redraw between two reads.
    return list(
        dashboard.page.evaluate(
            """() => {
                const boxes = [...document.querySelectorAll('.af-wire-node')];
                const found = [];
                const rects = boxes.map(b => [b.dataset.node, b.getBoundingClientRect()]);
                for (const [i, [a, r]] of rects.entries()) {
                    for (const [b, s] of rects.slice(i + 1)) {
                        if (r.left < s.right && s.left < r.right && r.top < s.bottom && s.top < r.bottom) {
                            found.push(a + ' over ' + b);
                        }
                    }
                }
                for (const box of boxes) {
                    if (box.scrollHeight > box.clientHeight + 1) found.push(box.dataset.node + ' overflows');
                }
                return found;
            }"""
        )
    )


@pytest.mark.usefixtures("installed")
def test_two_agents_are_joined_by_their_topic(
    dashboard: browser.Dashboard, unexpected: guard.Guard
) -> None:
    _signed_in_again(dashboard, unexpected)
    dashboard.reload()
    _opened(dashboard)

    assert _node(dashboard, "Agent e2e-ping").count() == 1
    assert _node(dashboard, "Topic e2e/pings").count() == 1
    nobody = _node(dashboard, "Topic e2e/nobody")
    assert "nobody reads this" in nobody.inner_text()
    assert _overlapping(dashboard) == []
    dashboard.assert_accessible("the wiring, as a graph")


@pytest.mark.usefixtures("installed")
def test_a_topic_chosen_from_the_keyboard_says_who_publishes_and_reads_it(
    dashboard: browser.Dashboard,
) -> None:
    _opened(dashboard)
    pings = _node(dashboard, "Topic e2e/pings")

    pings.focus()
    dashboard.page.keyboard.press("Enter")

    waiting.until(
        lambda: _node(dashboard, "Topic e2e/pings").get_attribute("aria-pressed") == "true",
        what="Enter to choose the topic",
    )
    detail = _view(dashboard).get_by_role("region", name="Selected")
    waiting.until(
        lambda: "Read bye2e-pong" in detail.inner_text().replace("\n", "").replace("\t", ""),
        what="the details to say who reads it",
    )
    # The focus stays on what was chosen, and Tab moves on to the next box.
    focused = dashboard.page.evaluate("() => document.activeElement?.dataset?.node || ''")
    assert focused == "topic:e2e/pings"
    dashboard.page.keyboard.press("Tab")
    moved = dashboard.page.evaluate("() => document.activeElement?.className || ''")
    assert "af-wire-node" in moved
    dashboard.assert_accessible("the wiring, a topic chosen")


@pytest.mark.usefixtures("installed")
def test_the_warning_shows_its_topic_alone_and_the_list_says_it_too(
    dashboard: browser.Dashboard,
) -> None:
    _opened(dashboard)
    # The chip alone: a topic's box says "nobody reads this" too.
    chip = _view(dashboard).get_by_role("button", name=re.compile(r"^\d+ topics? nobody reads$"))

    chip.click()

    waiting.until(
        lambda: _node(dashboard, "Topic e2e/pings").count() == 0,
        what="the warning to show only its topics",
    )
    assert _node(dashboard, "Topic e2e/nobody").count() == 1
    chip.click()
    _view(dashboard).get_by_role("button", name="View as list").click()
    warnings = _view(dashboard).get_by_role("list", name="Warnings")
    warnings.get_by_role("button", name="Nobody reads e2e/nobody").wait_for(state="visible")
    assert _view(dashboard).locator(".af-wire-frame").count() == 0
    dashboard.assert_accessible("the wiring, as a list")


@pytest.mark.usefixtures("installed")
def test_a_box_dragged_stays_where_it_was_put(dashboard: browser.Dashboard) -> None:
    _opened(dashboard)
    pong = _node(dashboard, "Agent e2e-pong")
    pong.scroll_into_view_if_needed()
    start = pong.bounding_box()
    assert start is not None
    mouse = dashboard.page.mouse

    mouse.move(start["x"] + 20, start["y"] + 10)
    mouse.down()
    mouse.move(start["x"] + 60, start["y"] + 70, steps=5)
    mouse.up()

    moved = _node(dashboard, "Agent e2e-pong").bounding_box()
    assert moved is not None and abs(moved["y"] - start["y"] - 60) <= 2, (start, moved)
    # The drag chose nothing; the place is kept in this browser.
    assert _node(dashboard, "Agent e2e-pong").get_attribute("aria-pressed") == "false"
    dashboard.reload()
    _opened(dashboard)
    again = _node(dashboard, "Agent e2e-pong").bounding_box()
    assert again is not None and abs(again["y"] - moved["y"]) <= 2, (moved, again)

    _view(dashboard).get_by_role("button", name="Reset layout").click()
    waiting.until(
        lambda: _view(dashboard).get_by_role("button", name="Reset layout").count() == 0,
        what="Reset layout to put the boxes back",
    )
