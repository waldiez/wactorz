"""A package installed beside Wactorz adds a tab to the dashboard: listed, it opens with
what the package serves, keeps its own place in the address, and is ended when another
view opens; left out of ``WACTORZ_EXTENSIONS``, it has no tab at all.

The package is real: a distribution's metadata in a folder on ``PYTHONPATH``,
registering an extension whose ``panel()`` names a script beside it.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import backend, browser, guard, waiting
from playwright.sync_api import Locator

DISTRIBUTION = "acme-panel"
TAB = "Acme"

MODULE = """
from pathlib import Path

from aiohttp import web

from wactorz.ext import Panel


async def _hello(_request):
    return web.json_response({"hello": "from the package"})


def setup(app):
    app.router.add_get("/api/acme/hello", _hello)


def panel(_app):
    return Panel(title="Acme", script=Path(__file__).with_name("acme_panel.js"))
"""

SCRIPT = """
export const apiVersion = 1;

export function mount(root, host) {
    const heading = document.createElement("h2");
    heading.textContent = "Acme";
    const said = document.createElement("p");
    const where = document.createElement("p");
    where.setAttribute("aria-label", "Where");
    const next = document.createElement("button");
    next.type = "button";
    next.textContent = "Runs";
    next.addEventListener("click", () => host.navigate(["runs"]));
    root.append(heading, said, where, next);
    const show = path => { where.textContent = "At: " + (path.join("/") || "the start"); };
    show(host.route());
    host.onRoute(show);
    void host.request("/api/acme/hello").then(answer => {
        said.textContent = answer.ok ? answer.value.hello : answer.error;
    });
    return () => { window.acmeEnded = (window.acmeEnded || 0) + 1; };
}
"""


@pytest.fixture(scope="module", name="site")
def site_fixture(app: backend.Backend) -> Path:
    assert app.run is not None
    site = app.run.root / "site"
    info = site / "acme_panel-1.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (site / "acme_panel.py").write_text(MODULE)
    (site / "acme_panel.js").write_text(SCRIPT)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[wactorz.extensions]\nacme = acme_panel\n")
    return site


@pytest.fixture(scope="module", name="listed")
def listed_fixture(app: backend.Backend, site: Path) -> Iterator[None]:
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


def _tab(dashboard: browser.Dashboard) -> Locator:
    nav = dashboard.page.get_by_role("navigation", name=browser.NAV, exact=True)
    return nav.get_by_role("button", name=TAB, exact=True)


@pytest.mark.usefixtures("listed")
def test_a_listed_package_opens_its_tab_with_what_it_serves(
    dashboard: browser.Dashboard, unexpected: guard.Guard
) -> None:
    _signed_in_again(dashboard, unexpected)
    dashboard.reload()

    _tab(dashboard).first.click()

    view = dashboard.page.get_by_test_id("view-acme")
    view.get_by_role("heading", name="Acme").wait_for(state="visible")
    view.get_by_text("from the package").wait_for(state="visible")
    assert dashboard.address == "#/acme"


@pytest.mark.usefixtures("listed")
def test_its_place_is_in_the_address(dashboard: browser.Dashboard) -> None:
    # Between tests the guard reads every thread, which leaves the tab.
    dashboard.open_address("#/acme")
    view = dashboard.page.get_by_test_id("view-acme")
    view.get_by_role("button", name="Runs").click()

    waiting.until(lambda: dashboard.address == "#/acme/runs", what="the address to follow the tab")
    dashboard.reload()
    waiting.until(
        lambda: dashboard.page.get_by_label("Where").first.inner_text() == "At: runs",
        what="a refresh to return to the same place in the tab",
    )


@pytest.mark.usefixtures("listed")
def test_another_view_ends_it(dashboard: browser.Dashboard) -> None:
    dashboard.open_address("#/acme")
    dashboard.page.get_by_test_id("view-acme").get_by_role("heading", name="Acme").wait_for()
    ended = dashboard.page.evaluate("() => window.acmeEnded || 0")

    dashboard.show("feed")

    waiting.until(
        lambda: dashboard.page.evaluate("() => window.acmeEnded") == ended + 1,
        what="the tab's own cleanup to run",
    )


def test_a_package_left_out_has_no_tab(
    app: backend.Backend, site: Path, dashboard: browser.Dashboard, unexpected: guard.Guard
) -> None:
    app.restart(extra={"PYTHONPATH": str(site)})
    _signed_in_again(dashboard, unexpected)
    dashboard.reload()

    assert _tab(dashboard).count() == 0
    assert app.rest.raw("GET", "/api/ext/acme/panel.js").status == 404
    app.restart()
