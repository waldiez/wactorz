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

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import waiting

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ConsoleMessage, Locator, Page

#: How long to look for a chat target that was on the list a moment ago before
#: taking it as gone. Long enough for a list being redrawn; far short of the wait
#: that a missing option otherwise costs.
GONE_MS = 2_000

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

#: Installed in the page by `Dashboard.watch_card`: records each change in
#: whether a card for the agent is on the page. Transitions rather than DOM
#: nodes, so the overview replacing a card with a fresh one in one step is not
#: counted as a going and a coming.
_WATCH_CARD = """
name => {
    const events = [];
    (window.__cardEvents = window.__cardEvents || {})[name] = events;
    const selector = `.af-card[data-name="${CSS.escape(name)}"]`;
    let present = document.querySelector(selector) !== null;
    if (present) {
        events.push("present");
    }
    new MutationObserver(() => {
        const now = document.querySelector(selector) !== null;
        if (now !== present) {
            events.push(now ? "added" : "removed");
            present = now;
        }
    }).observe(document.body, { childList: true, subtree: true });
}
"""
HISTORY_BUTTON = "button:has-text('History')"
HISTORY_PANEL = ".af-trend-panel"
CHAT_INPUT = "#af-iobar-input"
SEND_BUTTON = ".af-send-btn"
TARGET_SELECT = "#af-target-select"
AGENT_MESSAGE = ".af-chat-msg-agent"
USER_MESSAGE = ".af-chat-msg-user"
MESSAGE_FROM = ".af-chat-msg-from"
MESSAGE_BODY = ".af-chat-msg-bubble"
WAITING_BODY = "af-chat-waiting"
NODE_LIST = "#af-node-list"
CONNECTION_BADGE = ".af-conn-badge"
CARD_NAME = ".af-card-name"
CARD_STATE = ".af-card-state-label"
CARD_ACTION = "[data-action='{action}']"
CARD_CHAT = ".af-chat-btn"
CONFIRM = ".af-confirm"
RESET_MENU = "Clear stored state"
RESET_MENU_OPEN = ".af-audio-popover.open"
RESET_ENTRY = ".af-audio-popover.open button"
#: Two presses clear something: one asks, one answers. More are allowed for a
#: machine slow enough that the question lapsed in between.
RESET_PRESSES_AT_MOST = 6
CONFIRM_TITLE = ".af-confirm-title"
CONFIRM_MESSAGE = ".af-confirm-message"
CONFIRM_OK = ".af-confirm-ok"
CONFIRM_CANCEL = ".af-confirm-cancel"
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

    def connection(self) -> str:
        """What the header says about the page's feed from the server."""
        return self.page.locator(CONNECTION_BADGE).first.inner_text().strip()

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

    def watch_card(self, name: str) -> Dashboard:
        """Start recording every time the agent's card appears or goes, on the overview.

        Recorded in the page, on every change to it, so a card that goes and
        comes back between two polls is caught. Read with `card_comings_and_goings`;
        stay on the overview meanwhile, since another view takes the cards away.
        """
        self.show("overview")
        self.page.evaluate(_WATCH_CARD, name)
        return self

    def card_comings_and_goings(self, name: str) -> list[str]:
        """What `watch_card` recorded for ``name``: "present", "added" and "removed", in order."""
        return list(self.page.evaluate("name => (window.__cardEvents || {})[name] || []", name))

    def wait_for_no_card(self, name: str, *, timeout: float = 60.0) -> Dashboard:
        self.show("overview")
        waiting.until(
            lambda: name not in self.card_names(),
            what=f"the card for {name!r} to go from the overview",
            timeout=timeout,
            interval=0.25,
        )
        return self

    def _card(self, name: str) -> Locator:
        """The card of the agent called exactly ``name``."""
        named = self.page.locator(CARD_NAME).get_by_text(name, exact=True)
        return self.page.locator(AGENT_CARD).filter(has=named)

    def card_state(self, name: str) -> str:
        """What the agent's card says its state is."""
        self.show("overview")
        return self._card(name).locator(CARD_STATE).inner_text().strip()

    def card_actions(self, name: str) -> list[str]:
        """The buttons the agent's card offers, by what each says."""
        self.show("overview")
        buttons = self._card(name).locator("button:visible").all_inner_texts()
        return [text.strip() for text in buttons]

    def press(self, name: str, action: str) -> Dashboard:
        """Press ``action`` (start, stop or delete) on the agent's card."""
        self.show("overview")
        self._card(name).locator(CARD_ACTION.format(action=action)).click()
        return self

    # ── The question before something that cannot be undone ────────────────

    def asked_to_confirm(self) -> tuple[str, str]:
        """The title and the message of the dialog on screen."""
        self.page.wait_for_selector(CONFIRM, state="visible")
        title = self.page.locator(CONFIRM_TITLE).inner_text().strip()
        return title, self.page.locator(CONFIRM_MESSAGE).inner_text().strip()

    def confirm(self) -> Dashboard:
        self.page.locator(CONFIRM_OK).click()
        self.page.wait_for_selector(CONFIRM, state="detached")
        return self

    def cancel(self) -> Dashboard:
        self.page.locator(CONFIRM_CANCEL).click()
        self.page.wait_for_selector(CONFIRM, state="detached")
        return self

    # ── Clearing what the install has stored ────────────────────────────────

    def clear(self, what: str) -> str:
        """Choose ``what`` in the header's "Clear stored state" menu, and confirm.

        The menu asks in place: the first press turns the entry into a question,
        and the second answers it. Returns the question it asked.
        """
        self.page.get_by_role("button", name=RESET_MENU, exact=True).click()
        menu = self.page.locator(RESET_MENU_OPEN)
        entry = self.page.locator(RESET_ENTRY).filter(has_text=what)
        asked = ""
        # The question lapses a few seconds after it is asked, and a press that
        # comes after that asks it again instead of answering. So: press until
        # the menu has closed, which is what an answer does to it.
        for _ in range(RESET_PRESSES_AT_MOST):
            entry.click()
            if menu.count() == 0:
                return asked
            asked = entry.inner_text().strip()
        raise AssertionError(f"the menu never took an answer to {asked!r}")

    def notices(self) -> list[str]:
        """The toasts on screen now, each as its title and its message."""
        return list(
            self.page.locator(TOAST).evaluate_all(
                """toasts => toasts.map(toast =>
                    (toast.querySelector('.wz-toast__name')?.innerText || '').trim() + ': ' +
                    (toast.querySelector('.wz-toast__message')?.innerText || '').trim())"""
            )
        )

    def node_names(self) -> set[str]:
        names = self.page.locator(f"{NODE_LIST} .af-node-name").all_inner_texts()
        return {name.strip() for name in names}

    def node_machine(self, name: str) -> str:
        """What the node's card says its machine is, or "" before its manifest arrives."""
        self.show("overview")
        machine = self.page.locator(f'.af-node-card[data-node="{name}"] .af-node-machine')
        return machine.inner_text().strip() if machine.count() else ""

    # ── History ─────────────────────────────────────────────────────────────

    def open_history(self, name: str, *, node: bool = False) -> str:
        """Open an agent's or a node's history from its card's History button.

        Returns what the panel shows once it has loaded: the titles of its
        charts, or the sentence it says instead when there is nothing to draw.
        """
        self.show("overview")
        card = self.page.locator(f'.af-node-card[data-node="{name}"]') if node else self._card(name)
        card.locator(HISTORY_BUTTON).click()
        self.page.wait_for_selector(HISTORY_PANEL, state="visible")
        self.page.wait_for_function(
            "() => !document.querySelector('.af-trend-body')?.textContent?.startsWith('Loading')"
        )
        titles = self.page.locator(f"{HISTORY_PANEL} figcaption").all_inner_texts()
        return ", ".join(titles) or self.page.locator(".af-trend-body").inner_text().strip()

    def history_title(self) -> str:
        """The name the open history panel is about."""
        return self.page.locator(f"{HISTORY_PANEL} h3").inner_text().strip()

    def close_history(self) -> Dashboard:
        """Close the history panel as a person would, with Escape."""
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector(HISTORY_PANEL, state="detached")
        return self

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

    def _every_thread(self) -> dict[str, list[Said]]:
        """What each agent's thread holds, by agent. Leaves the screen as it was."""
        self.show("chat")
        was = self.talking_to
        threads: dict[str, list[Said]] = {}
        for agent in self._targets():
            # An agent can leave the list between reading it and choosing it:
            # one on a node goes when the node does. Checking first narrows that
            # window without closing it, so a choice that finds the option gone
            # moves on rather than waiting out Playwright's default for it.
            if agent not in self._targets():
                continue
            try:
                self.page.locator(TARGET_SELECT).select_option(agent, timeout=GONE_MS)
            except PlaywrightTimeoutError:
                continue
            threads[agent] = self.said()
        if was in self._targets():
            self.read_thread_of(was)
        return threads

    def all_unclaimed(self) -> dict[str, list[Said]]:
        """Agent messages no scenario has said it expected, in every thread, by agent."""
        pending = {
            agent: said[self._claimed.get(agent, 0) :]
            for agent, said in self._every_thread().items()
        }
        return {agent: said for agent, said in pending.items() if said}

    def all_said(self) -> list[Said]:
        """Every agent message in every thread."""
        return [said for thread in self._every_thread().values() for said in thread]

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
