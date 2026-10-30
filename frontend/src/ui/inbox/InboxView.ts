/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Inbox: what the agents are waiting on the person for (`#/inbox`), and
 * what was answered lately (`#/inbox/answered`).
 *
 * A question sent with `notify_user` alone keeps no book, so it is not here: it
 * shows as a toast and in its agent's thread.
 *
 * Waiting cards are moved, never rebuilt: a new book must not take the focus or
 * a half-typed note from the person answering.
 */
import type { InboxState, QuestionInbox } from "../../questions/QuestionInbox";
import { formatRoute } from "../../router";
import { timeLabel, toMs } from "../../time";
import type { LedgerQuestion } from "../../types/agent";
import { el, labelled, list } from "../dom";
import { answeredCard, QuestionCard } from "./questionCard";

/** The address's level for the answered list. */
export const ANSWERED_TAB = "answered";

/** Where the two lists are. */
function tabHref(tab: string): string {
    return formatRoute({ view: "inbox", path: tab ? [tab] : [] });
}

/** One of the two lists, as a row of the side panel that is a link to it. */
class ListRow {
    /** The row's element. */
    readonly element: HTMLAnchorElement;
    private readonly _count = el("span", "af-inbox-count");
    private readonly _line = el("span", "af-model-row-line");

    constructor(label: string, tab: string) {
        this.element = el("a", "af-model-row");
        this.element.href = tabHref(tab);
        this.element.append(el("span", "af-model-row-name", label), this._count, this._line);
    }

    /** Show `count` and `line`, marked as the list open when `current`. */
    show(count: string, line: string, current: boolean): void {
        this._count.textContent = count;
        this._line.textContent = line;
        this.element.classList.toggle("open", current);
        if (current) {
            this.element.setAttribute("aria-current", "page");
        } else {
            this.element.removeAttribute("aria-current");
        }
    }
}

/** `prefix` and the time `at`, or `none` when there is no time to give. */
function moment(prefix: string, at: number | undefined, none: string): string {
    return at ? `${prefix} ${timeLabel(toMs(at))}` : none;
}

export class InboxView {
    private readonly _root = el("div", "af-inbox");
    private readonly _waitingTab = new ListRow("Waiting", "");
    private readonly _answeredTab = new ListRow("Answered", ANSWERED_TAB);
    private readonly _lists = el("div", "af-inbox-lists");
    private readonly _waiting = list(el("ul", "af-inbox-list"), "Questions waiting for you");
    private readonly _answered = list(el("ul", "af-inbox-list"), "Questions answered");
    private readonly _none = el("p", "af-inbox-none");
    /** The waiting cards on screen, by question id. */
    private readonly _cards = new Map<string, QuestionCard>();
    private _tab = "";
    private _unsubscribe: (() => void) | null = null;

    constructor(
        private readonly inbox: QuestionInbox,
        /** How the page names the agent that asked. */
        private readonly label: (question: LedgerQuestion) => string,
    ) {
        const tabs = labelled(el("nav", "af-inbox-tabs"), "Inbox lists");
        tabs.append(this._waitingTab.element, this._answeredTab.element);
        const side = el("aside", "af-inbox-side");
        side.append(el("h2", "af-models-title", "Questions"), tabs);
        const heading = el("header", "af-inbox-heading");
        heading.append(
            el("h1", "af-inbox-title", "Inbox"),
            el("p", "af-inbox-lead", "What the agents are waiting on you for, and what you answered."),
        );
        this._lists.append(this._waiting, this._answered, this._none);
        const columns = el("div", "af-inbox-columns");
        columns.append(side, this._lists);
        this._root.append(heading, columns);
    }

    /** The list open, for the address: "" for what is waiting. */
    get tab(): string {
        return this._tab;
    }

    /** Open the list the address asks for; any other level is what is waiting. */
    show(tab: string): void {
        this._tab = tab === ANSWERED_TAB ? ANSWERED_TAB : "";
        this.redraw();
    }

    /** The view's element, kept across redraws of the dashboard. */
    build(): HTMLElement {
        this._unsubscribe ??= this.inbox.subscribe(() => this.redraw());
        this.redraw();
        return this._root;
    }

    /** Draw what the inbox holds, how agents are named included. */
    redraw(): void {
        const state = this.inbox.state;
        const answering = this._tab === "";
        const count = (shown: LedgerQuestion[]) => (state.read || shown.length ? `${shown.length}` : "–");
        this._waitingTab.show(
            count(state.open),
            moment("oldest", state.open[0]?.asked_at, "nothing waiting"),
            answering,
        );
        this._answeredTab.show(
            count(state.answered),
            moment("latest", state.answered[0]?.answered_at, "none yet"),
            !answering,
        );
        this._waiting.hidden = !answering;
        this._answered.hidden = answering;
        this._drawWaiting(state.open);
        this._answered.replaceChildren(
            ...state.answered.map(question => answeredCard(question, this.label(question))),
        );
        const shown = answering ? state.open : state.answered;
        this._none.hidden = shown.length > 0;
        this._none.textContent = this._emptyLine(state, answering);
    }

    /** Stop following the inbox (the dashboard goes). */
    destroy(): void {
        this._unsubscribe?.();
        this._unsubscribe = null;
    }

    /** What an empty list says. */
    private _emptyLine(state: InboxState, answering: boolean): string {
        if (state.failed) {
            return "Could not read the questions. They are read again when the connection is back.";
        }
        if (!state.read) {
            return "Reading the questions…";
        }
        return answering ? "Nothing is waiting for you." : "Nothing answered yet.";
    }

    /** Put a card per question in order, moving only what is out of place. */
    private _drawWaiting(open: LedgerQuestion[]): void {
        const wanted = new Set(open.map(question => question.id));
        for (const [id, card] of this._cards) {
            if (!wanted.has(id)) {
                card.element.remove();
                this._cards.delete(id);
            }
        }
        open.forEach((question, index) => {
            const card = this._cards.get(question.id) ?? this._newCard(question);
            card.refresh(question);
            const here = this._waiting.children[index] ?? null;
            if (here !== card.element) {
                this._waiting.insertBefore(card.element, here);
            }
        });
    }

    private _newCard(question: LedgerQuestion): QuestionCard {
        const card = new QuestionCard(question, {
            label: asked => this.label(asked),
            answer: (asked, option, note) => this.inbox.answer(asked, option, note),
        });
        this._cards.set(question.id, card);
        return card;
    }
}
