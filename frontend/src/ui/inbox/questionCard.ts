/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A question in the Inbox, waiting or answered.
 *
 * The waiting card is kept across redraws: the agents' books arrive whenever
 * anything changes, and a note half typed must survive them. No option is
 * drawn as the primary one, as in the toast: the question may be a permission,
 * which is not to be granted by the button the eye lands on first.
 */
import { formatRoute } from "../../router";
import { timeLabel, toMs } from "../../time";
import type { LedgerQuestion } from "../../types/agent";
import { button, el, labelled, listItem, named } from "../dom";

/** The most a note may hold, as the server keeps it. */
const MAX_NOTE_CHARS = 500;

/** What a card needs from the view. */
export interface CardHost {
    /** How the page names the agent that asked: `name@node` where the name is shared. */
    label(question: LedgerQuestion): string;
    /** Send the pick. Returns the server's refusal, or "" once it is sent. */
    answer(question: LedgerQuestion, option: string, note: string): Promise<string>;
}

/** The line above a question: who asked, a link to their thread, and when. */
function whoAsked(question: LedgerQuestion, label: string, at: number | undefined): HTMLElement {
    const line = el("div", "af-inbox-who");
    const link = el("a", "af-inbox-agent", label);
    link.href = formatRoute({ view: "chat", path: [label] });
    line.append(link);
    if (at) {
        line.append(el("span", "af-inbox-when", timeLabel(toMs(at))));
    }
    if (question.channel && question.channel !== "dashboard") {
        line.append(el("span", "af-inbox-channel", `asked on ${question.channel}`));
    }
    return line;
}

/** What the agent said about why it asks, if it said. */
function why(question: LedgerQuestion): HTMLElement[] {
    return question.context ? [el("p", "af-inbox-why", question.context)] : [];
}

/** A question that was answered: what it asked, what was picked and said, and where. */
export function answeredCard(question: LedgerQuestion, label: string): HTMLElement {
    const card = listItem(el("li", "af-inbox-card"), `${label}: ${question.text}`);
    const elsewhere = question.answer_channel && question.answer_channel !== "dashboard";
    card.append(
        whoAsked(question, label, question.answered_at),
        el("p", "af-inbox-text", question.text),
        ...why(question),
        el(
            "p",
            "af-inbox-answer",
            `Answered: ${question.answer ?? ""}${elsewhere ? ` (on ${question.answer_channel})` : ""}`,
        ),
    );
    if (question.answer_note) {
        card.append(el("p", "af-inbox-note", question.answer_note));
    }
    return card;
}

/** A question waiting for an answer: its options, and a note to send with one. */
export class QuestionCard {
    /** The card's element. */
    readonly element: HTMLElement;
    private readonly _who = el("div", "af-inbox-who-slot");
    private readonly _note = named(el("textarea", "af-inbox-note-input"), "note", "A note with your answer");
    private readonly _refusal = el("p", "af-inbox-refusal");
    private readonly _buttons: HTMLButtonElement[];
    private _question: LedgerQuestion;

    constructor(
        question: LedgerQuestion,
        private readonly host: CardHost,
    ) {
        this._question = question;
        this.element = listItem(el("li", "af-inbox-card"), question.text);
        this._note.maxLength = MAX_NOTE_CHARS;
        this._note.rows = 2;
        this._refusal.setAttribute("role", "alert");
        this._refusal.hidden = true;
        this._buttons = question.options.map(option => {
            const pick = button("af-inbox-option", option);
            pick.addEventListener("click", () => void this._pick(option));
            return pick;
        });
        const options = labelled(el("div", "af-inbox-options"), "Answers");
        options.setAttribute("role", "group");
        options.append(...this._buttons);
        const noting = el("details", "af-inbox-noting");
        noting.append(el("summary", "", "Add a note"), this._note);
        this.element.append(
            this._who,
            el("p", "af-inbox-text", question.text),
            ...why(question),
            options,
            noting,
            this._refusal,
        );
        this.refresh(question);
    }

    /** Show `question` as it now stands, keeping what was typed. */
    refresh(question: LedgerQuestion): void {
        this._question = question;
        const label = this.host.label(question);
        this.element.setAttribute("aria-label", `${label}: ${question.text}`);
        this._who.replaceChildren(whoAsked(question, label, question.asked_at));
    }

    /** Send `option` with the note; on a refusal, say why and offer the options again. */
    private async _pick(option: string): Promise<void> {
        this._buttons.forEach(pick => (pick.disabled = true));
        this._refusal.hidden = true;
        const refused = await this.host.answer(this._question, option, this._note.value.trim());
        if (refused) {
            this._refusal.textContent = refused;
            this._refusal.hidden = false;
            this._buttons.forEach(pick => (pick.disabled = false));
        }
    }
}
