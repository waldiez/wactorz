"""Preconditions, and the fixtures every scenario is built from.

A missing prerequisite is an error here, never a skip. A run without a broker, or
without a browser, checks nothing at all, and reporting that as a pass is worse
than reporting nothing - it is the failure mode where a suite is green for a
month because it stopped running. So the preconditions fail the session, and each
failure names the one command that fixes it.

Skips exist, and they are for a different thing: hardware and services that are
allowed to be absent - a Home Assistant, a real edge node, a paid model. Those
skip loudly, and never red.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import profiles
import pytest
from harness import backend, broker, browser, node, probe

if TYPE_CHECKING:
    # Deferred at runtime for the same reason `harness.browser` defers it: this
    # module is imported to run the precondition that reports a missing
    # Playwright as one line rather than as an ImportError at collection.
    from playwright.sync_api import Playwright

E2E_ROOT = Path(__file__).resolve().parent
OUT = E2E_ROOT / "out"
SCRIPTS = E2E_ROOT / "scripts"


# ── Command line ────────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--profile",
        default=None,
        help="test | rehearse | demo (default: $E2E_PROFILE, else test)",
    )
    parser.addoption(
        "--headed",
        action="store_true",
        default=False,
        help=(
            "Show the browser, whatever the profile says. Watching a scenario is "
            "useful under `test` too, and `rehearse` also changes pacing, capture "
            "and the script - this changes only whether you can see it. "
            "Also readable as $E2E_HEADED."
        ),
    )
    parser.addoption(
        "--resolution",
        default=None,
        metavar="NAME",
        help=(
            "What to render and record at: "
            + " | ".join(sorted(browser.RESOLUTIONS))
            + f" (default: {browser.DEFAULT_RESOLUTION}). Also readable as $E2E_RESOLUTION."
        ),
    )
    parser.addoption(
        "--real-node",
        default=None,
        metavar="NAME",
        help=(
            "Consent to reset and redeploy this configured deploy target, so "
            "@pytest.mark.requires_node scenarios run against real hardware. "
            "Also readable as $E2E_REAL_NODE."
        ),
    )


# ── Preconditions ───────────────────────────────────────────────────────────


def _refuse(message: str) -> None:
    """Stop the whole session, with the fix in the message."""
    pytest.exit(f"\ne2e preconditions not met:\n  {message}\n", returncode=1)


def pytest_configure(config: pytest.Config) -> None:
    """Everything that must be true before a single scenario is collected.

    Checked here rather than in a fixture so a run that cannot check anything
    stops before it starts pretending to, and so the message is the first thing
    on screen instead of eleven copies of the same error.
    """
    # A state directory exported for ordinary work is the leak this rule exists
    # to catch. Refused rather than used: the suite mints its own per run, and a
    # run that wrote into someone's real state directory would be discovered by
    # the damage rather than by the failure.
    inherited = os.environ.get("WACTORZ_STATE_DIR", "").strip()
    if inherited:
        _refuse(
            f"WACTORZ_STATE_DIR is set to {inherited!r}. This suite mints a fresh state\n"
            f"  directory per run and must not touch an existing one. Unset it, or use the\n"
            f"  `make e2e*` targets, which unset it for you."
        )

    try:
        chosen = profiles.resolve(config.getoption("--profile"))
    except ValueError as exc:
        _refuse(str(exc))
        return
    # One override, applied after the profile is chosen rather than as a fourth
    # profile: "show me the browser" is orthogonal to pacing, capture and which
    # model answers, and a profile per combination is how three become twelve.
    headed = config.getoption("--headed") or os.getenv("E2E_HEADED", "").strip() not in ("", "0")
    if headed and chosen.headless:
        chosen = replace(chosen, headless=False)

    wanted = (config.getoption("--resolution") or os.getenv("E2E_RESOLUTION", "")).strip().lower()
    if wanted:
        if wanted not in browser.RESOLUTIONS:
            # Refused rather than defaulted, for the same reason an unknown
            # profile is: a typo that silently records at the wrong size is only
            # discovered by looking at the file afterwards.
            known = ", ".join(sorted(browser.RESOLUTIONS))
            _refuse(f"unknown resolution {wanted!r} - choose one of: {known}")
        chosen = replace(chosen, size=browser.RESOLUTIONS[wanted])

    config.stash[_PROFILE_KEY] = chosen

    if not broker.reachable():
        _refuse(f"no MQTT broker on {broker.HOST}:{broker.PORT}. Start one with `make dev`.")

    if not browser.installed():
        _refuse(
            "the Playwright browser is not installed. Run `make e2e-setup` once.\n"
            "  Browser scenarios are part of the regression core, so this is not optional."
        )


_PROFILE_KEY = pytest.StashKey[profiles.Profile]()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the availability markers, so each one skips loudly for its own reason."""
    chosen = config.stash[_PROFILE_KEY]
    real_node = config.getoption("--real-node") or os.getenv("E2E_REAL_NODE", "")

    for item in items:
        for mark in item.iter_markers(name="requires_llm"):
            wanted = (mark.args[0] if mark.args else "real").lower()
            if wanted == "real" and chosen.uses_fake_model:
                item.add_marker(
                    pytest.mark.skip(
                        reason=(
                            f"needs a real model; the {chosen.name!r} profile runs the fake one. "
                            f"Run `make e2e-demo` to exercise this."
                        )
                    )
                )
        if list(item.iter_markers(name="requires_ha")) and not (
            os.getenv("HA_URL") and os.getenv("HA_TOKEN")
        ):
            item.add_marker(
                pytest.mark.skip(reason="needs a live Home Assistant: set HA_URL and HA_TOKEN")
            )
        if list(item.iter_markers(name="requires_node")) and not real_node:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "needs real hardware, and consent to reset it: name a configured "
                        "deploy target with --real-node=<name> or $E2E_REAL_NODE"
                    )
                )
            )


# ── The run ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", name="profile")
def profile_fixture(pytestconfig: pytest.Config) -> profiles.Profile:
    return pytestconfig.stash[_PROFILE_KEY]


@pytest.fixture(scope="session", name="real_node")
def real_node_fixture(pytestconfig: pytest.Config) -> str:
    """The deploy target this run was given consent to reset, or ""."""
    return pytestconfig.getoption("--real-node") or os.getenv("E2E_REAL_NODE", "")


@pytest.fixture(scope="session", name="run_id")
def run_id_fixture() -> str:
    """Sortable, and unique enough that two runs never share a directory."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


#: How many previous runs' artefacts are kept. Everything here exists to explain
#: a failure, and a failure from six runs ago has been explained or forgotten.
#: Videos are the reason there is a number at all: they are megabytes each and
#: the recording profiles write one per browser scenario, so an unpruned tree
#: grows without limit for as long as anyone keeps rehearsing.
KEEP_RUNS = 5


def prune(directory: Path, keep: int = KEEP_RUNS) -> None:
    """Delete all but the newest `keep` run directories under `directory`.

    By name, not by mtime: run directories are named with a sortable UTC stamp,
    and mtime is whatever the last write happened to touch - which on a kept
    failure is not the run that failed.
    """
    if not directory.is_dir():
        return
    runs = sorted((child for child in directory.iterdir() if child.is_dir()), key=lambda c: c.name)
    for stale in runs[:-keep] if keep else runs:
        shutil.rmtree(stale, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True, name="pruned")
def pruned_fixture() -> None:
    """Trim old runs before this one adds to them.

    At the start rather than at the end, so the newest run is never the one
    pruned, and so an interrupted run still gets tidied by the next one.
    """
    for kind in ("state", "logs", "videos", "traces"):
        prune(OUT / kind)


@pytest.fixture(scope="session", name="run_out")
def run_out_fixture(run_id: str) -> Path:
    directory = OUT / "state" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session", name="logs_dir")
def logs_dir_fixture(run_id: str) -> Path:
    directory = OUT / "logs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session", name="traces_dir")
def traces_dir_fixture(run_id: str) -> Path:
    directory = OUT / "traces" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session", name="videos_dir")
def videos_dir_fixture(profile: profiles.Profile, run_id: str) -> Path | None:
    if not profile.video:
        return None
    directory = OUT / "videos" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session", name="script")
def script_fixture(profile: profiles.Profile) -> str:
    """The fake provider's script for this profile, as JSON.

    Empty for a profile on a real model, and empty for a script file that is not
    there - the fake's own default answers everything, and a scenario asserting
    on exact wording would be a scenario that cannot run on both providers.
    """
    if not profile.uses_fake_model:
        return ""
    path = SCRIPTS / profile.script
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── The shared backend ──────────────────────────────────────────────────────


@pytest.fixture(scope="session", name="app")
def app_fixture(
    profile: profiles.Profile, run_out: Path, logs_dir: Path, script: str
) -> Iterator[backend.Backend]:
    """One backend for the whole run, settled before the first scenario sees it.

    Session-scoped on purpose. Starting the application takes real seconds, and
    the scenarios that share this are the ones whose claims build on each other -
    an agent spawned in `a04` is the agent talked to in `a05` and cycled in `a07`.
    That is also why the scenario files are numbered: they run in file order, and
    the order is part of the story.

    The scenarios that must not share it - the one that stops the process, and
    the one that wipes the state directory - ask for `own_app` instead.
    """
    instance = backend.start(
        state_dir=run_out / "app",
        console_log=logs_dir / "app.log",
        llm=profile.llm,
        script=script,
    )
    try:
        backend.wait_until_settled(instance)
        yield instance
    finally:
        instance.kill()


@pytest.fixture(name="api")
def api_fixture(app: backend.Backend) -> probe.Rest:
    """The REST API of the shared backend."""
    return app.rest


@pytest.fixture(name="own_app")
def own_app_fixture(
    profile: profiles.Profile,
    run_out: Path,
    logs_dir: Path,
    script: str,
    request: pytest.FixtureRequest,
) -> Iterator[Callable[..., backend.Backend]]:
    """A factory for a backend of one's own, torn down with the scenario.

    For the scenarios that stop, wipe or misconfigure the thing they are testing.
    Each gets a state directory named after the test, so a failure leaves an
    artefact whose name says which scenario produced it.
    """
    started: list[backend.Backend] = []

    def make(**overrides: object) -> backend.Backend:
        label = f"{request.node.name}-{len(started)}"
        settings: dict[str, object] = {
            "state_dir": run_out / label,
            "console_log": logs_dir / f"{label}.log",
            "llm": profile.llm,
            "script": script,
        }
        settings.update(overrides)
        instance = backend.start(**settings)  # type: ignore[arg-type]
        started.append(instance)
        return instance

    try:
        yield make
    finally:
        for instance in started:
            instance.kill()


# ── Nodes ───────────────────────────────────────────────────────────────────


@pytest.fixture(name="edge_node")
def edge_node_fixture(
    run_out: Path, logs_dir: Path, request: pytest.FixtureRequest
) -> Iterator[Callable[..., node.Node]]:
    """A factory for edge-node runners, all stopped with the scenario."""
    started: list[node.Node] = []

    def make(name: str = "") -> node.Node:
        chosen = name or f"e2e-{request.node.name.replace('_', '-')}-{len(started)}"
        instance = node.start(
            name=chosen,
            workdir=run_out / "nodes" / chosen,
            console_log=logs_dir / f"node-{chosen}.log",
        )
        started.append(instance)
        return instance

    try:
        yield make
    finally:
        for instance in started:
            instance.stop()


# ── The browser ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", name="playwright_driver")
def playwright_driver_fixture() -> Iterator[Playwright]:
    """One driver for the whole run - see `harness.browser.driver`."""
    with browser.driver() as instance:
        yield instance


@pytest.fixture(name="dashboard")
def dashboard_fixture(
    app: backend.Backend,
    profile: profiles.Profile,
    videos_dir: Path | None,
    traces_dir: Path,
    playwright_driver: Playwright,
    request: pytest.FixtureRequest,
) -> Iterator[browser.Dashboard]:
    """The dashboard of the shared backend, open and drawn.

    Keeps a Playwright trace when the scenario failed and throws it away when it
    passed - a browser failure is close to undebuggable from a log alone, and a
    trace of every green run is hundreds of megabytes nobody opens.
    """
    with browser.browser_page(
        playwright_driver,
        base_url=app.url,
        headless=profile.headless,
        video_dir=videos_dir,
        dwell_seconds=profile.dwell,
        size=profile.size,
    ) as page:
        opened = page.open()
        yield opened
        if _failed(request.node):
            opened.save_trace(traces_dir / f"{request.node.name}.zip")
        else:
            opened.discard_trace()


@pytest.fixture(name="browse")
def browse_fixture(
    profile: profiles.Profile, videos_dir: Path | None, playwright_driver: Playwright
) -> Iterator[Callable[[backend.Backend], browser.Dashboard]]:
    """A dashboard pointed at a backend of the scenario's choosing.

    For the scenarios that run their own backend and still want a browser -
    `a10`, which watches the system come back empty after a reset.
    """
    stack = contextlib.ExitStack()

    def make(instance: backend.Backend) -> browser.Dashboard:
        page = stack.enter_context(
            browser.browser_page(
                playwright_driver,
                base_url=instance.url,
                headless=profile.headless,
                video_dir=videos_dir,
                dwell_seconds=profile.dwell,
                size=profile.size,
            )
        )
        return page.open()

    try:
        yield make
    finally:
        stack.close()


# ── After the run ───────────────────────────────────────────────────────────


_FAILED_KEY = pytest.StashKey[bool]()
_ITEM_FAILED_KEY = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Remember what failed, so each teardown can decide what evidence to keep.

    Recorded at two levels because two different decisions read it: a browser
    fixture keeps its own trace when *its* test failed, and the run's state
    directory survives when *anything* did.

    `wrapper=True` rather than the older `hookwrapper=True`: the newer protocol
    yields the report itself and expects it back, which is the difference between
    a hook that can be given a return type and one whose result is `Any`.
    """
    report = yield
    if report.when in ("setup", "call") and report.failed:
        item.stash[_ITEM_FAILED_KEY] = True
        item.session.stash[_FAILED_KEY] = True
    return report


def _failed(item: pytest.Item) -> bool:
    return item.stash.get(_ITEM_FAILED_KEY, False)


@pytest.fixture(scope="session", autouse=True)
def keep_artefacts_on_failure(request: pytest.FixtureRequest, run_out: Path) -> Iterator[None]:
    """Clean the run's state directory on green, keep every byte on red.

    The asymmetry is the point: a passing run leaves nothing to wade through, and
    a failing one leaves the state, the console captures and the videos that
    explain it. Logs and videos are never deleted - they are small, and the run
    that produced an interesting recording is often the one that passed.
    """
    yield
    failed = request.session.stash.get(_FAILED_KEY, False)
    if failed:
        print(f"\ne2e: run artefacts kept at {run_out}")
        return
    shutil.rmtree(run_out, ignore_errors=True)
