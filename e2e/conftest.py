"""The install every journey runs against, and what is checked after each.

One install for the whole run: a broker, the application as a process, and a
browser signed in to it. They are started once, in that order, and the journeys
then use them one after another, each starting from what the last one left.

After every journey the guard reads what happened that nobody asked for (see
``harness/guard.py``). It runs as part of the journey, so what it finds fails
that journey and is reported with it.

A run that passes removes its directory under ``e2e/out``. One that fails keeps
it: the backend's console, the broker's, and a browser trace.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# The harness is imported as `harness`, by the journeys too: this suite is run
# from its own folder, with its own pytest.ini, and is not part of the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import backend, broker, browser, guard, node
from harness import run as runs

import model  # isort: skip

_FAILED = pytest.StashKey[bool]()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_FAILED] = False
    if shutil.which("docker") is None:
        pytest.exit("\nthe e2e suite starts its broker with docker, which is not on PATH\n", 1)
    try:
        import playwright  # noqa: F401  # an optional extra: `make e2e-setup` installs it
    except ImportError:
        pytest.exit("\nthe e2e suite needs Playwright: run `make e2e-setup` once\n", 1)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    report = yield
    if report.failed:
        item.config.stash[_FAILED] = True
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Run the journey, then the guard, as one thing that passes or fails."""
    watching = item.funcargs.get("unexpected") if hasattr(item, "funcargs") else None
    if watching is not None:
        watching.begin()
    result = yield
    if watching is not None:
        watching.check()
    return result


@pytest.fixture(scope="session", name="run")
def run_fixture(pytestconfig: pytest.Config) -> Iterator[runs.Run]:
    run = runs.new()
    yield run
    if pytestconfig.stash[_FAILED]:
        print(f"\ne2e: this run's logs and trace are kept in {run.root}")
    else:
        shutil.rmtree(run.root, ignore_errors=True)


@pytest.fixture(scope="session", name="app")
def app_fixture(run: runs.Run) -> Iterator[backend.Backend]:
    """The broker, a machine to deploy to, and the application, for the whole run.

    What it runs once it has settled is recorded on it (`started_with`): the
    journeys that compare against a fresh install compare against that, so what
    a server starts by default can change without them being edited to match.
    """
    script = model.as_json()
    node.make_key(run)
    broker.issue_files(run, backend.environment(run, script=script))
    try:
        broker.up(run)
        node.up(run)
        app = backend.start(run, script=script)
        app.started_with = frozenset(str(agent.get("name")) for agent in app.rest.agents())
        try:
            yield app
        finally:
            app.kill()
    finally:
        (run.logs / "broker.log").write_text(broker.log(run), encoding="utf-8")
        (run.logs / "node.log").write_text(node.log(), encoding="utf-8")
        (run.logs / "node-machine.log").write_text(node.machine_log(), encoding="utf-8")
        broker.down(run)


@pytest.fixture(scope="session", name="chromium")
def chromium_fixture() -> Iterator[object]:
    from playwright.sync_api import sync_playwright  # the optional extra

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            yield chromium
        finally:
            chromium.close()


@pytest.fixture(scope="session", name="dashboard")
def dashboard_fixture(
    chromium: object, app: backend.Backend, run: runs.Run, pytestconfig: pytest.Config
) -> Iterator[browser.Dashboard]:
    """One tab, signed in, kept open for the whole run as a person's would be."""
    context = chromium.new_context(  # type: ignore[attr-defined]
        viewport={"width": browser.WIDTH, "height": browser.HEIGHT}
    )
    context.tracing.start(screenshots=True, snapshots=True)
    dashboard = browser.Dashboard(page=context.new_page(), base_url=app.url, context=context)
    try:
        dashboard.sign_in(run.api_key)
        yield dashboard
    finally:
        if pytestconfig.stash[_FAILED]:
            dashboard.save_trace(run.traces / "dashboard.zip")
        else:
            dashboard.discard_trace()
        context.close()


@pytest.fixture(name="visitor")
def visitor_fixture(chromium: object, app: backend.Backend) -> Iterator[browser.Dashboard]:
    """A browser that has never been here: no session, nothing stored."""
    context = chromium.new_context()  # type: ignore[attr-defined]
    try:
        yield browser.Dashboard(page=context.new_page(), base_url=app.url)
    finally:
        context.close()


@pytest.fixture(autouse=True, name="unexpected")
def unexpected_fixture(app: backend.Backend, dashboard: browser.Dashboard) -> guard.Guard:
    """The guard for this journey. Ask it to `allow` an error a journey provokes."""
    return guard.Guard(console=app.console_log, dashboard=dashboard)
