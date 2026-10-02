"""The dashboard, as a person uses it, with nothing it shows left unread.

One page object, so that when the dashboard's markup moves one file changes.
Every method acts or waits on a condition; a scenario has nowhere to write a
sleep.

**Every agent message is accounted for.** A scenario says what it expects an
agent to answer, word for word, with :meth:`Dashboard.expect`. The chat keeps a
thread per agent, the one the composer is addressed to being the one on screen,
and each is counted on its own. Whatever is said in any of them that no scenario
claimed is in :meth:`Dashboard.all_unclaimed` when the scenario ends, and fails
it (see ``conftest.py``). A suite that only waits for
"an answer" passes when the answer is "I could not do that"; this one reads it.

Selectors are the ids and classes the dashboard ships, listed in one block so a
rename shows up as a change to a list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import waiting

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ConsoleMessage, Page

WIDTH, HEIGHT = 1280, 800

#: The dashboard is one page with these views, not one URL each.
VIEWS = ("overview", "chat", "feed", "settings")

#: What each view puts on screen when it has really drawn itself.
VIEW_CONTENT = {
    "overview": "#af-stats-grid",
    "chat": "#af-chat-thread",
    "feed": "#af-feed-view",
    "settings": "#af-cost-period",
}

LOGIN_KEY = "#key"
LOGIN_SUBMIT = "button[type=submit]"
NAV_BUTTON = ".af-view-btn[data-view='{view}']"
AGENT_CARD = ".af-card[data-id]"
CHAT_INPUT = "#af-iobar-input"
SEND_BUTTON = ".af-send-btn"
TARGET_SELECT = "#af-target-select"
AGENT_MESSAGE = ".af-chat-msg-agent"
USER_MESSAGE = ".af-chat-msg-user"
MESSAGE_FROM = ".af-chat-msg-from"
MESSAGE_BODY = ".af-chat-msg-bubble"
WAITING_BODY = "af-chat-waiting"
NODE_LIST = "#af-node-list"
TOAST = ".wz-toast"

#: How long an agent is given to say what a scenario expects of it.
REPLY_TIMEOUT = 60.0


@dataclass(frozen=True)
class Said:
    """One agent message in the thread: who it is from, and its words."""

    sender: str
    text: str

    def __str__(self) -> str:
        return f"{self.sender}: {self.text}"


class UnexpectedReply(AssertionError):
    """An agent said something other than what the scenario expected of it."""


@dataclass
class Dashboard:
    """One browser page, signed in to one backend."""

    page: Page
    base_url: str
    context: BrowserContext | None = None
    #: How many agent messages a scenario has claimed, in each agent's thread.
    _claimed: dict[str, int] = field(default_factory=dict)
    #: Errors the page itself reported: its console, and exceptions nothing caught.
    page_errors: list[str] = field(default_factory=list)
    #: Error toasts, collected as they appear: a toast is gone again in seconds.
    error_toasts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.page.on("console", self._on_console)
        self.page.on("pageerror", lambda error: self.page_errors.append(f"uncaught: {error}"))

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.page_errors.append(f"console: {message.text}")

    # ── Getting there ───────────────────────────────────────────────────────

    def sign_in(self, key: str) -> Dashboard:
        """Open the dashboard, be sent to the sign-in page, and sign in with ``key``."""
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.wait_for_selector(LOGIN_KEY, state="visible")
        self.page.fill(LOGIN_KEY, key)
        self.page.click(LOGIN_SUBMIT)
        self._wait_until_drawn()
        return self

    def reload(self) -> Dashboard:
        self.page.reload(wait_until="domcontentloaded")
        self._wait_until_drawn()
        return self

    def _wait_until_drawn(self) -> None:
        # The page builds its own DOM, so the document finishing loading says
        # nothing about there being anything on screen.
        self.page.wait_for_selector(NAV_BUTTON.format(view="overview"), state="visible")
        self.page.wait_for_selector(AGENT_CARD, state="visible")

    @property
    def at_sign_in(self) -> bool:
        return self.page.locator(LOGIN_KEY).count() == 1

    def show(self, view: str) -> Dashboard:
        self.page.locator(NAV_BUTTON.format(view=view)).first.click()
        self.page.wait_for_selector(VIEW_CONTENT[view], state="visible")
        return self

    # ── Agents ──────────────────────────────────────────────────────────────

    def card_names(self) -> set[str]:
        names = self.page.locator(f"{AGENT_CARD} .af-card-name").all_inner_texts()
        return {name.strip() for name in names}

    def wait_for_card(self, name: str, *, timeout: float = 60.0) -> Dashboard:
        self.show("overview")
        waiting.until(
            lambda: name in self.card_names(),
            what=f"a card for {name!r} on the overview",
            timeout=timeout,
            interval=0.25,
        )
        return self

    def wait_for_no_card(self, name: str, *, timeout: float = 60.0) -> Dashboard:
        self.show("overview")
        waiting.until(
            lambda: name not in self.card_names(),
            what=f"the card for {name!r} to go from the overview",
            timeout=timeout,
            interval=0.25,
        )
        return self

    def node_names(self) -> set[str]:
        names = self.page.locator(f"{NODE_LIST} .af-node-name").all_inner_texts()
        return {name.strip() for name in names}

    # ── The chat ────────────────────────────────────────────────────────────

    def say(self, message: str, *, to: str) -> Dashboard:
        """Type a message to ``to`` and send it, the way a person does."""
        self.show("chat")
        waiting.until(
            lambda: to in self._targets(),
            what=f"{to!r} to be offered as someone to talk to",
            timeout=60.0,
            interval=0.25,
        )
        self.read_thread_of(to)
        self.page.locator(CHAT_INPUT).fill(message)
        self.page.locator(SEND_BUTTON).click()
        waiting.until(
            lambda: message in self.sent(),
            what=f"{message!r} to appear in the thread as sent",
            interval=0.1,
        )
        return self

    def read_thread_of(self, agent: str) -> Dashboard:
        """Address the composer to ``agent``, which puts its thread on screen."""
        self.show("chat")
        self.page.locator(TARGET_SELECT).select_option(agent)
        return self

    @property
    def talking_to(self) -> str:
        """The agent whose thread is on screen."""
        return self.page.locator(TARGET_SELECT).input_value()

    def _targets(self) -> list[str]:
        options = self.page.locator(f"{TARGET_SELECT} option")
        return list(options.evaluate_all("options => options.map(o => o.value)"))

    def sent(self) -> list[str]:
        """What the user has said, as the thread shows it."""
        bodies = self.page.locator(f"{USER_MESSAGE} {MESSAGE_BODY}").all_inner_texts()
        return [body.strip() for body in bodies]

    def said(self) -> list[Said]:
        """Every agent message in the thread, in order, without one still being awaited."""
        rows = self.page.locator(AGENT_MESSAGE).evaluate_all(
            """rows => rows.map(row => {
                const body = row.querySelector('.af-chat-msg-bubble');
                return {
                    sender: (row.querySelector('.af-chat-msg-from')?.innerText || '').trim(),
                    text: (body?.innerText || '').trim(),
                    waiting: !!body && body.classList.contains('af-chat-waiting'),
                };
            })"""
        )
        return [Said(row["sender"], row["text"]) for row in rows if not row["waiting"]]

    def unclaimed(self) -> list[Said]:
        """Agent messages in the thread on screen that no scenario has said it expected."""
        return self.said()[self._claimed.get(self.talking_to, 0) :]

    def all_unclaimed(self) -> dict[str, list[Said]]:
        """The same for every agent's thread, by agent. Leaves the screen as it was."""
        self.show("chat")
        was = self.talking_to
        found: dict[str, list[Said]] = {}
        for agent in self._targets():
            self.read_thread_of(agent)
            pending = self.unclaimed()
            if pending:
                found[agent] = pending
        self.read_thread_of(was)
        return found

    def all_said(self) -> list[Said]:
        """Every agent message in every thread."""
        self.show("chat")
        was = self.talking_to
        everything: list[Said] = []
        for agent in self._targets():
            everything += self.read_thread_of(agent).said()
        self.read_thread_of(was)
        return everything

    def expect(self, sender: str, text: str, *, timeout: float = REPLY_TIMEOUT) -> Said:
        """The next agent message in the thread on screen is from ``sender`` and says exactly ``text``.

        Waits for it, since a reply arrives a piece at a time. It fails as soon
        as the next message is from someone else or has said something ``text``
        does not begin with, and quotes what was said.
        """
        return self._expect(sender, lambda said: said == text, repr(text), text, timeout)

    def expect_like(self, sender: str, pattern: str, *, timeout: float = REPLY_TIMEOUT) -> Said:
        """As :meth:`expect`, for a message with a part that differs between runs.

        ``pattern`` is a regular expression the whole message must match. For a
        duration, an address or an identifier; not a way to stop reading.
        """
        matcher = re.compile(pattern, re.DOTALL)
        return self._expect(
            sender, lambda said: matcher.fullmatch(said) is not None, f"/{pattern}/", None, timeout
        )

    def _expect(self, sender, matches, wanted: str, prefix_of: str | None, timeout: float) -> Said:
        def arrived() -> Said | None:
            pending = self.unclaimed()
            if not pending:
                return None
            first = pending[0]
            if first.sender != sender:
                raise UnexpectedReply(f"expected {sender} to say {wanted}, but next came {first}")
            if matches(first.text):
                return first
            if prefix_of is not None and not prefix_of.startswith(first.text):
                raise UnexpectedReply(
                    f"expected {sender} to say {wanted}, but it said {first.text!r}"
                )
            return None

        try:
            found = waiting.until(
                arrived,
                what=f"{sender} to say {wanted}",
                timeout=timeout,
                interval=0.1,
                giving_up_on=UnexpectedReply,
            )
        except waiting.ConditionTimeout as exc:
            pending = self.unclaimed()
            so_far = f"; so far it has said {pending[0].text!r}" if pending else "; nothing came"
            raise UnexpectedReply(f"{exc}{so_far}") from exc
        thread = self.talking_to
        self._claimed[thread] = self._claimed.get(thread, 0) + 1
        return found

    # ── What the page complains about ───────────────────────────────────────

    def note_toasts(self) -> None:
        """Remember any error toast on screen now. Called often: they do not stay."""
        # Read in one step inside the page: a toast can go between two reads.
        shown = self.page.locator(TOAST).evaluate_all(
            """toasts => toasts.map(toast => ({
                kind: (toast.querySelector('.wz-toast__badge')?.innerText || '').trim().toLowerCase(),
                text: (toast.querySelector('.wz-toast__message')?.innerText || '').trim(),
            }))"""
        )
        for toast in shown:
            if toast["kind"] == "error" and toast["text"] not in self.error_toasts:
                self.error_toasts.append(toast["text"])

    # ── Evidence ────────────────────────────────────────────────────────────

    def save_trace(self, path: Path) -> None:
        """Write the Playwright trace: `playwright show-trace <file>` replays every step."""
        if self.context is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.context.tracing.stop(path=str(path))

    def discard_trace(self) -> None:
        if self.context is not None:
            self.context.tracing.stop()
