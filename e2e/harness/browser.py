"""The dashboard, as a person uses it.

One page object rather than selectors scattered through scenarios. Not for tidiness
- for the rule that a scenario may express no timing. Every method here either
acts or waits on a condition, so there is nowhere in a scenario for a sleep to be
written; and when the dashboard's markup moves, one file changes rather than
eleven.

Selectors are the ids and classes the dashboard actually ships. They are listed
in one block below so a rename shows up as a diff to a list rather than as a
scenario that mysteriously stops finding things.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import waiting

if TYPE_CHECKING:
    # Under TYPE_CHECKING because the runtime import is deliberately deferred -
    # see `driver`. Real names rather than `Any`: these objects have large APIs,
    # and a page object that types them gets told when a Playwright upgrade moves
    # something, which is the whole reason to have a page object.
    from playwright.sync_api import BrowserContext, Page, Playwright

#: What a recording can be made at, by name. The page and the video are always
#: the same size as each other - a viewport that differs from the video size is
#: letterboxed into it - so one entry here sets both.
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

DEFAULT_RESOLUTION = "720p"

#: The default, kept as names because most of the harness only ever wants these.
WIDTH, HEIGHT = RESOLUTIONS[DEFAULT_RESOLUTION]

#: How much bigger a window has to be than the page inside it - the frame, the
#: tab strip and the toolbar. Measured on Chromium here (1280x720 of page needed
#: a 1312x849 window); it is an allowance, not a guarantee, and being a little
#: generous costs nothing. Affects only what a headed run looks like on screen:
#: the page renders at WIDTH x HEIGHT whether or not the window agrees.
CHROME_WIDTH = 32
CHROME_HEIGHT = 129

#: The dashboard is one page with four views, not four URLs, so "every page
#: renders" means "every view renders". These are the nav buttons' `data-view`.
VIEWS = ("overview", "chat", "feed", "settings")

#: What each view must actually put on screen for it to count as rendered. A view
#: that swaps in an empty container passes a "the button worked" check and fails
#: this one, which is the difference the scenario cares about.
VIEW_CONTENT = {
    "overview": "#af-stats-grid",
    "chat": "#af-chat-thread",
    "feed": "#af-feed-view",
    "settings": "#af-cost-period",
}

NAV_BUTTON = ".af-view-btn[data-view='{view}']"
AGENT_CARD = ".af-card[data-id]"
CHAT_INPUT = "#af-iobar-input"
SEND_BUTTON = ".af-send-btn"
TARGET_SELECT = "#af-target-select"
CHAT_THREAD = "#af-chat-thread"
AGENT_MESSAGE = ".af-chat-msg-agent"
USER_MESSAGE = ".af-chat-msg-user"
CHAT_MESSAGE = ".af-chat-msg"
NODE_LIST = "#af-node-list"


@dataclass
class Dashboard:
    """One browser page, pointed at one backend.

    `dwell` is carried here rather than passed to every call: the profile decides
    it once, and a scenario that writes `dwell="readable"` is expressing an intent
    about a recording, never a wait that anything depends on.
    """

    page: Page
    base_url: str
    #: Profile.dwell - an intent name in, seconds out.
    dwell_seconds: Callable[[str], float]
    #: The browser context, held only so a trace can be written on failure.
    #: Scenarios never touch it - everything they need is a method here.
    context: BrowserContext | None = None

    # ── Evidence ────────────────────────────────────────────────────────────

    def save_trace(self, path: Path) -> None:
        """Write the Playwright trace of everything that happened on this page.

        Kept only for a failure. A trace is large and there is one per browser
        scenario, so recording every green run costs hundreds of megabytes to
        preserve nothing anyone will open. Traces are why a browser failure is
        debuggable at all after the fact: `playwright show-trace <file>` replays
        the DOM at every step, which no amount of log reading substitutes for.
        """
        if self.context is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.context.tracing.stop(path=str(path))

    def discard_trace(self) -> None:
        if self.context is not None:
            self.context.tracing.stop()

    # ── Getting there ───────────────────────────────────────────────────────

    def open(self) -> Dashboard:
        """Load the dashboard and wait until it has drawn itself.

        Waits for the nav rather than for `load`: the page is a script that builds
        its own DOM, so the document finishing has nothing to do with there being
        anything on screen.
        """
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.wait_for_selector(NAV_BUTTON.format(view="overview"), state="visible")
        return self

    def reload(self) -> Dashboard:
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(NAV_BUTTON.format(view="overview"), state="visible")
        return self

    def show(self, view: str, *, dwell: str = "") -> Dashboard:
        """Switch to a view and wait for its content, not just its button."""
        if view not in VIEWS:
            raise ValueError(f"unknown view {view!r} - the dashboard has: {', '.join(VIEWS)}")
        self.page.click(NAV_BUTTON.format(view=view))
        self.page.wait_for_selector(VIEW_CONTENT[view], state="visible")
        self._dwell(dwell)
        return self

    def renders(self, view: str) -> bool:
        return self.page.locator(VIEW_CONTENT[view]).count() > 0

    # ── The overview ────────────────────────────────────────────────────────

    def card_names(self) -> set[str]:
        """Every agent that currently has a card, by the name shown on it."""
        return {
            (text or "").strip()
            for text in self.page.locator(f"{AGENT_CARD} .af-card-name").all_inner_texts()
        }

    def has_card(self, name: str) -> bool:
        return name in self.card_names()

    def wait_for_card(self, name: str, *, timeout: float = 60.0, dwell: str = "") -> Dashboard:
        waiting.until(
            lambda: self.has_card(name),
            what=f"a card for {name!r} on the overview",
            timeout=timeout,
            interval=0.25,
        )
        self._dwell(dwell)
        return self

    def card_state(self, name: str) -> str:
        """The state label on one agent's card, as a person reads it."""
        card = self.page.locator(AGENT_CARD).filter(has_text=name).first
        if card.count() == 0:
            return ""
        label = card.locator(".af-card-state-label")
        return (label.inner_text() or "").strip().lower() if label.count() else ""

    def node_names(self) -> set[str]:
        return {
            (text or "").strip()
            for text in self.page.locator(f"{NODE_LIST} .af-node-name").all_inner_texts()
        }

    # ── The chat ────────────────────────────────────────────────────────────

    def chat(self, message: str, *, to: str = "", dwell: str = "") -> Dashboard:
        """Type a message and send it, the way a person does.

        `fill` then click, rather than an API call: the point of a browser
        scenario is that the composer, the socket and the server agree, and a
        scenario that posts to the API tests none of that.

        `to` names the recipient, and a scenario that cares where its message
        lands should always give it. Without it the message goes wherever the
        composer happens to be pointed - which is the agent the dashboard picked
        by itself, and that default is currently decided by a race (see
        `.local/plans/chat-target-default.md`). A scenario that leans on it is
        one that talks to `main` most of the time and the catalogue agent the
        rest, and passes either way because something always answers.

        Deliberately not defaulted to `main` here. The page object must be able
        to observe the default as it is, or the scenario that exists to catch
        that bug would be asserting against a workaround.
        """
        if to:
            self.choose_target(to)
        self.show("chat")
        box = self.page.locator(CHAT_INPUT)
        box.fill(message)
        self._dwell(dwell)
        self.page.locator(SEND_BUTTON).click()
        return self

    def messages(self) -> list[str]:
        return [t.strip() for t in self.page.locator(CHAT_MESSAGE).all_inner_texts()]

    def replies(self) -> list[str]:
        return [t.strip() for t in self.page.locator(AGENT_MESSAGE).all_inner_texts()]

    def wait_for_reply(self, *, after: int = 0, timeout: float = 120.0, dwell: str = "") -> str:
        """Wait for one more agent message than there were, and return its text.

        Counted rather than matched, because an assertion on what the model said
        cannot hold under both providers - which is the rule that keeps a demo
        scenario runnable as a test. What holds under both is that an answer
        arrived.
        """
        waiting.until(
            lambda: len(self.replies()) > after,
            what="the agent's reply to appear in the thread",
            timeout=timeout,
            interval=0.25,
        )
        self._dwell(dwell)
        return self.replies()[-1]

    def target(self) -> str:
        """The agent the composer is currently addressed to."""
        return self.page.locator(TARGET_SELECT).input_value()

    def choose_target(self, name: str, *, dwell: str = "") -> Dashboard:
        self.show("chat")
        waiting.until(
            lambda: name in self.target_options(),
            what=f"{name!r} to be offered as a chat target",
            timeout=60.0,
            interval=0.25,
        )
        self.page.locator(TARGET_SELECT).select_option(name)
        self._dwell(dwell)
        return self

    def target_options(self) -> list[str]:
        return list(
            self.page.locator(f"{TARGET_SELECT} option").evaluate_all(
                "options => options.map(o => o.value)"
            )
        )

    def toasts(self) -> list[str]:
        """Whatever the dashboard is currently telling the user, in words."""
        return [t.strip() for t in self.page.locator(".af-toast, .toast").all_inner_texts()]

    # ── Pacing ──────────────────────────────────────────────────────────────

    def _dwell(self, intent: str) -> None:
        if intent:
            waiting.dwell(self.dwell_seconds(intent))


@contextlib.contextmanager
def driver() -> Generator[Playwright]:
    """The one Playwright driver a run gets.

    One, and shared, because the synchronous API refuses to start a second while
    a first is open in the same thread - it sees the running loop and tells you
    to use the async API instead. That is reached the moment two browser fixtures
    overlap, which is not exotic: a scenario holding a page open while opening a
    second page against a restarted backend does it. Owning the driver once at
    session scope makes the overlap ordinary, and makes every context after the
    first cheap.
    """
    # Function-local because it must not be required to *import* this module:
    # conftest imports `browser` at collection time to run the precondition
    # check, and at module scope a missing package would fail there with a bare
    # ImportError instead of the message naming `make e2e-setup`.
    from playwright.sync_api import sync_playwright

    instance = sync_playwright().start()
    try:
        yield instance
    finally:
        instance.stop()


@contextlib.contextmanager
def browser_page(
    driver: Playwright,
    *,
    base_url: str,
    headless: bool,
    video_dir: Path | None,
    dwell_seconds: Callable[[str], float],
    size: tuple[int, int] = (WIDTH, HEIGHT),
) -> Generator[Dashboard]:
    """One browser, one page, pointed at one backend.

    Video is a directory rather than a flag: Playwright writes the file when the
    context closes and names it itself, so a profile that records simply says
    where, and a profile that does not passes None.
    """
    width, height = size
    instance = driver.chromium.launch(
        headless=headless,
        # Sizes the actual window so a headed run shows the whole page without
        # scrollbars. The page itself is `size` whatever the window does - this
        # only decides how much of it you can see while it happens, and it is the
        # one number here that does not affect the recording. At 1080p on a 1080p
        # screen the window cannot fit and the desktop clamps it; the page is
        # still rendered and recorded at full size, you simply cannot watch all
        # of it at once.
        args=[f"--window-size={width + CHROME_WIDTH},{height + CHROME_HEIGHT}"],
    )
    # Fixed in both modes, and equal to the recording size. Letting the page take
    # its size from the window instead (`no_viewport`) is what put grey bars down
    # the side and along the bottom of every recording: Playwright scales the page
    # into the video frame preserving aspect ratio, so any window whose inner area
    # is not exactly WIDTH x HEIGHT gets letterboxed. Resizing the window after the
    # page exists does not rescue it either - recording starts with the page.
    context_args: dict[str, Any] = {"viewport": {"width": width, "height": height}}
    if video_dir is not None:
        video_dir.mkdir(parents=True, exist_ok=True)
        context_args["record_video_dir"] = str(video_dir)
        context_args["record_video_size"] = {"width": width, "height": height}
    context = instance.new_context(**context_args)
    # Started unconditionally and thrown away on success: tracing has to be
    # running before the first action to have anything to show, and whether
    # the scenario failed is not known until after the last one.
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    page = context.new_page()
    try:
        yield Dashboard(page=page, base_url=base_url, dwell_seconds=dwell_seconds, context=context)
    finally:
        context.close()
        instance.close()


#: Asks Playwright where its browser is and whether that file is really there.
#: Run in a subprocess - see `installed`.
_INSTALLED_CHECK = """
import pathlib, sys
from playwright.sync_api import sync_playwright

with sync_playwright() as driver:
    sys.exit(0 if pathlib.Path(driver.chromium.executable_path).exists() else 1)
"""


def installed() -> bool:
    """Whether Playwright and its browser are both actually present.

    Both, because they fail at different times and only one of them is obvious:
    the package missing is an ImportError at collection, while the browser missing
    is an exception on first launch, in the middle of a scenario, saying something
    about a driver. The precondition turns the second into the first.

    Asked in a subprocess rather than here. Starting Playwright's driver in the
    pytest process just to ask it a question leaves an event loop behind that
    complains on the way out, so every run - passing or failing - ended with a
    stack trace about a destroyed task that had nothing to do with the run.
    """
    result = subprocess.run(
        [sys.executable, "-c", _INSTALLED_CHECK],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result.returncode == 0
