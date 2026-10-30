/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The questions agents have asked, as the Inbox shows them: one book per agent.
 *
 * An agent publishes its whole book each time a question is asked or answered,
 * so a book heard replaces what was held for that agent. The ledger, read each
 * time the page goes live, replaces every book, and so brings in what was
 * missed while away. A read can cross a change: a book heard, or an answer sent
 * from here, after the read began is newer than what the read returns, and is kept.
 *
 * No DOM: the view reads `state` and is told when it changes.
 */
import type { Answer } from "../io/api";
import type { ListedQuestions } from "../io/openQuestions";
import type { LedgerQuestion, QuestionBook } from "../types/agent";

/** How many answered questions are shown, as the server's ledger keeps. */
export const ANSWERED_SHOWN = 100;

/** What there is to show. */
export interface InboxState {
    /** The questions waiting for an answer, oldest first. */
    open: LedgerQuestion[];
    /** The newest answered, newest first. */
    answered: LedgerQuestion[];
    /** Whether the ledger has been read. */
    read: boolean;
    /** Whether the last read of the ledger failed. */
    failed: boolean;
}

/** Told each time the state changes. */
export type InboxListener = (state: InboxState) => void;

/** One agent's questions, and when they were heard (`Date.now()`, 0 for a read of the ledger). */
interface Book {
    questions: LedgerQuestion[];
    heardAt: number;
}

/** Whether `question` still waits for an answer. */
function isOpen(question: LedgerQuestion): boolean {
    return typeof question.answer !== "string";
}

export class QuestionInbox {
    private readonly _books = new Map<string, Book>();
    private _state: InboxState = { open: [], answered: [], read: false, failed: false };
    private readonly _listeners = new Set<InboxListener>();

    constructor(
        private readonly api: {
            answer(question: LedgerQuestion, option: string, note: string): Promise<Answer<null>>;
        },
    ) {}

    /** The state as it stands. Replaced, never changed in place. */
    get state(): InboxState {
        return this._state;
    }

    /** Be told each change; returns the way to stop. */
    subscribe(listener: InboxListener): () => void {
        this._listeners.add(listener);
        return () => this._listeners.delete(listener);
    }

    /** Take an agent's book as it now stands. */
    heard(book: QuestionBook): void {
        this._books.set(book.agentId, { questions: book.questions, heardAt: Date.now() });
        this._publish({ read: this._state.read, failed: this._state.failed });
    }

    /** Take the ledger, keeping any book heard since the read of it began. */
    listed({ questions, askedAt }: ListedQuestions): void {
        const kept = [...this._books].filter(([, book]) => book.heardAt >= askedAt);
        this._books.clear();
        questions.forEach(question => {
            const book = this._books.get(question.agent_id) ?? { questions: [], heardAt: 0 };
            book.questions.push(question);
            this._books.set(question.agent_id, book);
        });
        kept.forEach(([agentId, book]) => this._books.set(agentId, book));
        this._publish({ read: true, failed: false });
    }

    /** The ledger could not be read: what is held stays, and the view says so if there is nothing. */
    notListed(): void {
        this._publish({ read: this._state.read, failed: true });
    }

    /**
     * Pick `option` for `question`, with `note` beside it. Returns the server's
     * refusal, or "" once it is sent; the question is then answered here, ahead
     * of the agent's book saying so.
     */
    async answer(question: LedgerQuestion, option: string, note: string): Promise<string> {
        const got = await this.api.answer(question, option, note);
        if (!got.ok) {
            return got.error;
        }
        this._settle(question, {
            answer: option,
            answer_channel: "dashboard",
            answered_at: Date.now() / 1000,
            ...(note && { answer_note: note }),
        });
        return "";
    }

    /** Write `answer` into the book holding `question`, if it still waits there. */
    private _settle(question: LedgerQuestion, answer: Partial<LedgerQuestion>): void {
        const book = this._books.get(question.agent_id);
        if (!book) {
            return;
        }
        const questions = book.questions.map(held =>
            held.id === question.id && isOpen(held) ? { ...held, ...answer } : held,
        );
        this._books.set(question.agent_id, { questions, heardAt: Date.now() });
        this._publish({ read: this._state.read, failed: this._state.failed });
    }

    private _publish(flags: Pick<InboxState, "read" | "failed">): void {
        const all = [...this._books.values()].flatMap(book => book.questions);
        this._state = {
            ...flags,
            open: all.filter(isOpen).sort((a, b) => (a.asked_at ?? 0) - (b.asked_at ?? 0)),
            answered: all
                .filter(question => !isOpen(question))
                .sort((a, b) => (b.answered_at ?? 0) - (a.answered_at ?? 0))
                .slice(0, ANSWERED_SHOWN),
        };
        this._listeners.forEach(listener => listener(this._state));
    }
}
