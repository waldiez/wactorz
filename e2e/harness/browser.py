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

Elements are found the way a person using a screen reader finds them: by role
and the name read out for them. What has no such name carries a test id, from
the dashboard's one list of them (``frontend/src/ui/testIds.ts``); a unit test
there checks that every id used here is on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import axe, waiting

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ConsoleMessage, Locator, Page

#: How long to look for a chat target that was on the list a moment ago before
#: taking it as gone. Long enough for a list being redrawn; far short of the wait
#: that a missing option otherwise costs.
GONE_MS = 2_000

WIDTH, HEIGHT = 1280, 800

#: The dashboard is one page with these views, not one URL each: each by the
#: words on its button.
VIEWS = {"overview": "Overview", "chat": "Chat", "feed": "Feed", "settings": "Settings"}

LOGIN_KEY = "API key"
LOGIN_SUBMIT = "Sign in"
NAV = "Views"
AGENTS = "Agents"
NODES = "Nodes"

#: Installed in the page by `Dashboard.watch_card`: records each change in
#: whether a card for the agent is on the page. Transitions rather than DOM
#: nodes, so the overview replacing a card with a fresh one in one step is not
#: counted as a going and a coming.
_WATCH_CARD = """
name => {
    const events = [];
    (window.__cardEvents = window.__cardEvents || {})[name] = events;
    const selector = `[data-testid="agent-card"][data-name="${CSS.escape(name)}"]`;
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
#: As `_WATCH_CARD`, for every card whose name starts with a prefix: for agents
#: whose names are made up as they start, such as a planner's.
_WATCH_CARDS_LIKE = """
prefix => {
    const seen = {};
    (window.__cardsLike = window.__cardsLike || {})[prefix] = seen;
    const named = () => new Set(
        [...document.querySelectorAll('[data-testid="agent-card"][data-name]')]
            .map(card => card.dataset.name)
            .filter(name => name.startsWith(prefix)),
    );
    let present = named();
    present.forEach(name => { seen[name] = ["present"]; });
    new MutationObserver(() => {
        const now = named();
        now.forEach(name => { if (!present.has(name)) (seen[name] = seen[name] || []).push("added"); });
        present.forEach(name => { if (!now.has(name)) (seen[name] = seen[name] || []).push("removed"); });
        present = now;
    }).observe(document.body, { childList: true, subtree: true });
}
"""
#: The agents each row of the nodes panel lists, by row: `local`, then each node.
_NODE_ROWS = """
() => {
    const listed = text => {
        const value = (text || "").trim();
        return value === "" || value === "no agents" ? [] : value.split(", ");
    };
    const rows = {};
    for (const row of document.querySelectorAll('[data-testid="node"]')) {
        rows[row.dataset.node] = listed(row.querySelector('[data-testid="node-agents"]')?.textContent);
    }
    return rows;
}
"""
#: Where keyboard focus is now, and whether it shows: what has it, and the
#: outline or shadow that marks it. ``None`` when nothing in the page has it.
_FOCUS = """
() => {
    const e = document.activeElement;
    if (!e || e === document.body) return null;
    const s = getComputedStyle(e);
    const name = e.getAttribute("aria-label") || e.innerText || e.getAttribute("placeholder") || "";
    return {
        what: `${e.tagName.toLowerCase()} ${JSON.stringify(name.trim().slice(0, 40))}`,
        shown: (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== "none",
    };
}
"""
#: How many Tab presses a view is given before its walk is cut short.
TAB_STOPS_AT_MOST = 80
#: The names of the items of a list, as assistive technology reads them.
_ITEM_NAMES = "items => items.map(item => item.getAttribute('aria-label'))"
CHAT_INPUT = "Chat message"
SEND_BUTTON = "Send message"
TARGET_SELECT = "Chat target agent"
RESET_MENU = "Clear stored state"
#: Two presses clear something: one asks, one answers. More are allowed for a
#: machine slow enough that the question lapsed in between.
RESET_PRESSES_AT_MOST = 6

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
        self.submit_key(key)
        self._wait_until_drawn()
        # A first visit opens the overview; its cards say the agents have loaded.
        self.page.get_by_test_id("agent-card").first.wait_for(state="visible")
        return self

    def submit_key(self, key: str) -> Dashboard:
        """Type ``key`` into the sign-in page and send it."""
        field = self.page.get_by_label(LOGIN_KEY, exact=True)
        field.wait_for(state="visible")
        field.fill(key)
        self.page.get_by_role("button", name=LOGIN_SUBMIT, exact=True).click()
        return self

    def reload(self) -> Dashboard:
        self.page.reload(wait_until="domcontentloaded")
        self._wait_until_drawn()
        return self

    def _wait_until_drawn(self) -> None:
        # The page builds its own DOM, so the document finishing loading says
        # nothing about there being anything on screen. Whichever view it opens:
        # the address, or the view last open, decides that.
        self._nav_button("overview").wait_for(state="visible")
        self.page.locator("[data-testid^='view-']").first.wait_for(state="visible")

    def open_address(self, hash_: str) -> Dashboard:
        """Load the dashboard at ``hash_`` (``#/feed``), as a link or a typed address does."""
        self.page.goto(f"{self.base_url.rstrip('/')}/{hash_}", wait_until="domcontentloaded")
        self._wait_until_drawn()
        return self

    def back(self) -> Dashboard:
        """Press the browser's Back button."""
        self.page.go_back(wait_until="commit")
        return self

    @property
    def view(self) -> str:
        """The view on screen, by its key."""
        mounted = self.page.locator("[data-testid^='view-']").first
        return (mounted.get_attribute("data-testid") or "").removeprefix("view-")

    @property
    def address(self) -> str:
        """The hash of the page's address: where a refresh returns to."""
        return str(self.page.evaluate("() => location.hash"))

    @property
    def at_sign_in(self) -> bool:
        return self.page.get_by_label(LOGIN_KEY, exact=True).count() == 1

    def connection(self) -> str:
        """What the header says about the page's feed from the server."""
        return self.page.get_by_test_id("connection").first.inner_text().strip()

    def _nav_button(self, view: str) -> Locator:
        """The button that opens ``view``, in whichever navigation is on screen."""
        nav = self.page.get_by_role("navigation", name=NAV, exact=True)
        return nav.get_by_role("button", name=VIEWS[view], exact=True).first

    def show(self, view: str) -> Dashboard:
        self._nav_button(view).click()
        self.page.get_by_test_id(f"view-{view}").wait_for(state="visible")
        return self

    # ── Agents ──────────────────────────────────────────────────────────────

    def card_names(self) -> set[str]:
        cards = self.page.get_by_role("list", name=AGENTS, exact=True).get_by_role("listitem")
        return set(cards.evaluate_all(_ITEM_NAMES))

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

    def watch_cards_named_like(self, prefix: str) -> Dashboard:
        """As `watch_card`, for every card whose name starts with ``prefix``."""
        self.show("overview")
        self.page.evaluate(_WATCH_CARDS_LIKE, prefix)
        return self

    def cards_named_like(self, prefix: str) -> dict[str, list[str]]:
        """What `watch_cards_named_like` recorded: each matching name's comings and goings."""
        return dict(self.page.evaluate("p => (window.__cardsLike || {})[p] || {}", prefix))

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
        agents = self.page.get_by_role("list", name=AGENTS, exact=True)
        return agents.get_by_role("listitem", name=name, exact=True)

    def card_state(self, name: str) -> str:
        """What the agent's card says its state is."""
        self.show("overview")
        return self._card(name).get_by_test_id("agent-state").inner_text().strip()

    def card_actions(self, name: str) -> list[str]:
        """The buttons the agent's card offers, by what each says."""
        self.show("overview")
        buttons = self._card(name).get_by_role("button").all_inner_texts()
        return [text.strip() for text in buttons]

    def press(self, name: str, action: str) -> Dashboard:
        """Press ``action`` (start, stop or delete) on the agent's card."""
        self.show("overview")
        label = f"{action.capitalize()} {name}"
        self._card(name).get_by_role("button", name=label, exact=True).click()
        return self

    # ── The question before something that cannot be undone ────────────────

    def asked_to_confirm(self) -> tuple[str, str]:
        """The title and the message of the dialog on screen."""
        dialog = self.page.get_by_test_id("confirm-dialog")
        dialog.wait_for(state="visible")
        title = dialog.get_by_test_id("confirm-title").inner_text().strip()
        return title, dialog.get_by_test_id("confirm-message").inner_text().strip()

    def confirm(self) -> Dashboard:
        return self._answer_dialog(self.page.get_by_test_id("confirm-ok"))

    def cancel(self) -> Dashboard:
        return self._answer_dialog(self.page.get_by_test_id("confirm-cancel"))

    def _answer_dialog(self, answer: Locator) -> Dashboard:
        answer.click()
        self.page.get_by_test_id("confirm-dialog").wait_for(state="detached")
        return self

    # ── Clearing what the install has stored ────────────────────────────────

    def clear(self, what: str) -> str:
        """Choose ``what`` in the header's "Clear stored state" menu, and confirm.

        The menu asks in place: the first press turns the entry into a question,
        and the second answers it. Returns the question it asked.
        """
        self.page.get_by_role("button", name=RESET_MENU, exact=True).click()
        # The menu is in the page while closed, but hidden: only an open one counts.
        menu = self.page.get_by_role("group", name=RESET_MENU, exact=True)
        entry = menu.get_by_role("button").filter(has_text=what)
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
            self.page.get_by_test_id("toast").evaluate_all(
                """toasts => toasts.map(toast =>
                    (toast.querySelector('[data-testid="toast-title"]')?.innerText || '').trim() + ': ' +
                    (toast.querySelector('[data-testid="toast-message"]')?.innerText || '').trim())"""
            )
        )

    def _node(self, name: str) -> Locator:
        """The row of the nodes panel for ``name``: `local`, or a node."""
        nodes = self.page.get_by_role("list", name=NODES, exact=True)
        return nodes.get_by_role("listitem", name=name, exact=True)

    def node_names(self) -> set[str]:
        rows = self.page.get_by_role("list", name=NODES, exact=True).get_by_role("listitem")
        return set(rows.evaluate_all(_ITEM_NAMES))

    def node_rows(self) -> dict[str, list[str]]:
        """The agents each row of the nodes panel lists: `local`, then each node by name."""
        self.show("overview")
        return dict(self.page.evaluate(_NODE_ROWS))

    def rows_listing(self, agent: str) -> set[str]:
        """The rows of the nodes panel that list ``agent``: `local`, a node's name, or none."""
        return {row for row, agents in self.node_rows().items() if agent in agents}

    def node_machine(self, name: str) -> str:
        """What the node's card says its machine is, or "" before its manifest arrives."""
        self.show("overview")
        machine = self._node(name).get_by_test_id("node-machine")
        return machine.inner_text().strip() if machine.count() else ""

    # ── History ─────────────────────────────────────────────────────────────

    def open_history(self, name: str, *, node: bool = False) -> str:
        """Open an agent's or a node's history from its card's History button.

        Returns what the panel shows once it has loaded: the titles of its
        charts, or the sentence it says instead when there is nothing to draw.
        """
        self.show("overview")
        card = self._node(name) if node else self._card(name)
        card.get_by_role("button", name=f"History of {name}", exact=True).click()
        panel = self.page.get_by_test_id("history-panel")
        panel.wait_for(state="visible")
        self.page.wait_for_function(
            """() => !document.querySelector('[data-testid="history-body"]')
                ?.textContent?.startsWith('Loading')"""
        )
        titles = panel.locator("figcaption").all_inner_texts()
        return ", ".join(titles) or panel.get_by_test_id("history-body").inner_text().strip()

    def history_title(self) -> str:
        """The name the open history panel is about."""
        return self.page.get_by_test_id("history-panel").get_by_role("heading").inner_text().strip()

    def close_history(self) -> Dashboard:
        """Close the history panel as a person would, with Escape."""
        self.page.keyboard.press("Escape")
        self.page.get_by_test_id("history-panel").wait_for(state="detached")
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
        self.page.get_by_role("textbox", name=CHAT_INPUT, exact=True).fill(message)
        self.page.get_by_role("button", name=SEND_BUTTON, exact=True).click()
        waiting.until(
            lambda: message in self.sent(),
            what=f"{message!r} to appear in the thread as sent",
            interval=0.1,
        )
        return self

    def read_thread_of(self, agent: str) -> Dashboard:
        """Address the composer to ``agent``, which puts its thread on screen."""
        self.show("chat")
        self._target_select().select_option(agent)
        return self

    def _target_select(self) -> Locator:
        return self.page.get_by_role("combobox", name=TARGET_SELECT, exact=True)

    @property
    def talking_to(self) -> str:
        """The agent whose thread is on screen."""
        return self._target_select().input_value()

    def _targets(self) -> list[str]:
        options = self._target_select().locator("option")
        return list(options.evaluate_all("options => options.map(o => o.value)"))

    def sent(self) -> list[str]:
        """What the user has said, as the thread shows it."""
        bodies = (
            self.page.get_by_test_id("user-message")
            .get_by_test_id("message-body")
            .all_inner_texts()
        )
        return [body.strip() for body in bodies]

    def said(self) -> list[Said]:
        """Every agent message in the thread, in order, without one still being awaited."""
        rows = self.page.get_by_test_id("agent-message").evaluate_all(
            """rows => rows.map(row => {
                const body = row.querySelector('[data-testid="message-body"]');
                return {
                    sender: (row.querySelector('[data-testid="message-from"]')?.innerText || '').trim(),
                    text: (body?.innerText || '').trim(),
                    waiting: row.querySelector('[data-testid="message-waiting"]') !== null,
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
                self._target_select().select_option(agent, timeout=GONE_MS)
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

    def threads_wiped(self) -> Dashboard:
        """Forget what was claimed: every thread is empty, and counts start again from it."""
        self._claimed.clear()
        return self

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

    # ── Questions an agent asks ─────────────────────────────────────────────

    def _question_toast(self, sender: str) -> Locator:
        # A toast that asks is a group named after the agent asking.
        return self.page.get_by_test_id("toast").and_(
            self.page.get_by_role("group", name=sender, exact=True)
        )

    def question_from(self, sender: str) -> str:
        """The question ``sender`` is asking in a notice on screen, or "" when there is none."""
        notice = self._question_toast(sender)
        if notice.count() == 0:
            return ""
        return notice.first.get_by_test_id("toast-message").inner_text().strip()

    def answer_notice(self, sender: str, option: str) -> Dashboard:
        """Press ``option`` on the notice of ``sender``'s question."""
        notice = self._question_toast(sender).first
        notice.get_by_role("button", name=option, exact=True).click()
        return self

    def thread_opens(self, agent: str) -> bool:
        """Whether ``agent``'s row in the chat list can be opened."""
        self.show("chat")
        return self._list_row(agent).is_enabled()

    def open_from_list(self, agent: str) -> Dashboard:
        """Open ``agent``'s thread from the chat list, the way to one that cannot be messaged."""
        self.show("chat")
        self._list_row(agent).click()
        waiting.until(
            lambda: self.page.get_by_test_id("chat-title").inner_text().strip() == f"@{agent}",
            what=f"{agent!r}'s thread to be on screen",
        )
        return self

    def _list_row(self, agent: str) -> Locator:
        """``agent``'s row in the chat's list of agents."""
        rows = self.page.get_by_test_id("chat-agent-list")
        return rows.get_by_role("button", name=agent, exact=True)

    def options_in_thread(self) -> list[str]:
        """The answers the thread on screen offers that can still be pressed."""
        buttons = self.page.get_by_test_id("chat-option")
        return [
            buttons.nth(i).inner_text().strip()
            for i in range(buttons.count())
            if buttons.nth(i).is_enabled()
        ]

    # ── What the page complains about ───────────────────────────────────────

    def tab_stops(self) -> list[tuple[str, bool]]:
        """Walk the page with Tab from the top: each stop, and whether focus shows on it.

        Ends where the walk comes round to its first stop again, or leaves the page.
        """
        self.page.evaluate("() => document.activeElement?.blur()")
        stops: list[tuple[str, bool]] = []
        for _ in range(TAB_STOPS_AT_MOST):
            self.page.keyboard.press("Tab")
            focus = self.page.evaluate(_FOCUS)
            if focus is None or (stops and focus["what"] == stops[0][0]):
                break
            stops.append((focus["what"], focus["shown"]))
        return stops

    def accessibility_findings(self) -> list[str]:
        """The serious and critical axe findings on screen now that nobody accepted."""
        return axe.blocking_findings(self.page)

    def assert_accessible(self, where: str) -> Dashboard:
        """Fail on any serious or critical axe finding on screen now, saying ``where`` it was."""
        found = self.accessibility_findings()
        assert not found, f"axe, on {where}:\n" + "\n".join(found)
        return self

    def note_toasts(self) -> None:
        """Remember any error toast on screen now. Called often: they do not stay."""
        # Read in one step inside the page: a toast can go between two reads.
        shown = self.page.get_by_test_id("toast").evaluate_all(
            """toasts => toasts.map(toast => ({
                kind: (toast.querySelector('[data-testid="toast-kind"]')?.innerText || '').trim().toLowerCase(),
                text: (toast.querySelector('[data-testid="toast-message"]')?.innerText || '').trim(),
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
