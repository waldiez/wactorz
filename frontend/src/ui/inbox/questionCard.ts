/**
 * SPDX-License-Identifier: Apache-2.0
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
    /** The avatar of the agent that asked. */
    face(question: LedgerQuestion): HTMLElement;
    /** Send the pick. Returns the server's refusal, or "" once it is sent. */
    answer(question: LedgerQuestion, option: string, note: string): Promise<string>;
    /** Send the words, for a question asked for them. The server's refusal, or "" once taken. */
    answerText(question: LedgerQuestion, text: string): Promise<string>;
}

/** The line above a question: who asked, with their avatar, a link to their thread, and when. */
function whoAsked(
    question: LedgerQuestion,
    label: string,
    at: number | undefined,
    face: HTMLElement,
): HTMLElement {
    const line = el("div", "af-inbox-who");
    const link = el("a", "af-inbox-agent", label);
    link.href = formatRoute({ view: "chat", path: [label] });
    line.append(face, link);
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
export function answeredCard(question: LedgerQuestion, label: string, face: HTMLElement): HTMLElement {
    const card = listItem(el("li", "af-inbox-card"), `${label}: ${question.text}`);
    const elsewhere = question.answer_channel && question.answer_channel !== "dashboard";
    card.append(
        whoAsked(question, label, question.answered_at, face),
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

/** The box a person types an answer in, for a question asked for words. */
function wordsBox(question: LedgerQuestion): HTMLTextAreaElement {
    const box = named(el("textarea", "af-inbox-words-input"), "answer", "Your answer");
    if (question.max_chars) {
        box.maxLength = question.max_chars;
    }
    box.rows = 4;
    return box;
}

/**
 * A question waiting for an answer: its options, and a note to send with one;
 * or, for one asked for words, a box to type them in and a button to send.
 */
export class QuestionCard {
    /** The card's element. */
    readonly element: HTMLElement;
    private readonly _who = el("div", "af-inbox-who-slot");
    private readonly _note = named(el("textarea", "af-inbox-note-input"), "note", "A note with your answer");
    private readonly _refusal = el("p", "af-inbox-refusal");
    private readonly _buttons: HTMLButtonElement[];
    /** What the person types, for a question asked for words; null for one with options. */
    private readonly _words: HTMLTextAreaElement | null;
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
        this._words = question.free_text ? wordsBox(question) : null;
        this._buttons = this._words ? [this._sendButton()] : this._optionButtons(question);
        this.element.append(
            this._who,
            el("p", "af-inbox-text", question.text),
            ...why(question),
            ...(this._words ? [this._wordsArea(this._words)] : this._options()),
            this._refusal,
        );
        this.refresh(question);
    }

    /** The box and, under it on the right, the button that sends what was typed. */
    private _wordsArea(box: HTMLTextAreaElement): HTMLElement {
        const area = el("div", "af-inbox-words");
        area.append(box, ...this._buttons);
        return area;
    }

    /** The button that sends what was typed: the card's one action. */
    private _sendButton(): HTMLButtonElement {
        const send = button("af-inbox-send", "Send");
        send.addEventListener("click", () => void this._send());
        return send;
    }

    /** A button per option, each sending its pick. */
    private _optionButtons(question: LedgerQuestion): HTMLButtonElement[] {
        return question.options.map(option => {
            const pick = button("af-inbox-option", option);
            pick.addEventListener("click", () => void this._pick(option));
            return pick;
        });
    }

    /** The option buttons as a group, and the note to send with one. */
    private _options(): HTMLElement[] {
        const options = labelled(el("div", "af-inbox-options"), "Answers");
        options.setAttribute("role", "group");
        options.append(...this._buttons);
        const noting = el("details", "af-inbox-noting");
        noting.append(el("summary", "", "Add a note"), this._note);
        return [options, noting];
    }

    /** Show `question` as it now stands, keeping what was typed. */
    refresh(question: LedgerQuestion): void {
        this._question = question;
        const label = this.host.label(question);
        this.element.setAttribute("aria-label", `${label}: ${question.text}`);
        this._who.replaceChildren(whoAsked(question, label, question.asked_at, this.host.face(question)));
    }

    /** Send `option` with the note; on a refusal, say why and offer the options again. */
    private async _pick(option: string): Promise<void> {
        await this._sending(() => this.host.answer(this._question, option, this._note.value.trim()));
    }

    /** Send what was typed; nothing to send says so, and a refusal keeps the words. */
    private async _send(): Promise<void> {
        const text = this._words?.value.trim() ?? "";
        if (!text) {
            this._say("Type an answer first.");
            return;
        }
        await this._sending(() => this.host.answerText(this._question, text));
    }

    /** Run `send` with the buttons off; on a refusal, say why and turn them on again. */
    private async _sending(send: () => Promise<string>): Promise<void> {
        this._buttons.forEach(pick => (pick.disabled = true));
        this._refusal.hidden = true;
        const refused = await send();
        if (refused) {
            this._say(refused);
            this._buttons.forEach(pick => (pick.disabled = false));
        }
    }

    /** Show `why` where a refusal is said. */
    private _say(why: string): void {
        this._refusal.textContent = why;
        this._refusal.hidden = false;
    }
}
