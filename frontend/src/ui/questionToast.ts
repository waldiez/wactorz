/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The toast for an agent's question: it stays up until the question is
 * answered, with a button per option.
 *
 * The thread the question is in may be one the user rarely opens, or one that
 * opens only while the question waits (an agent that cannot be messaged, such
 * as the installer asking about a package), so the toast is where it is seen
 * and can be answered. A press sends the pick as the thread's card does. No
 * option is drawn as the primary one: the question may be a permission, which
 * is not to be granted by the button the eye lands on first.
 */
import type { QuestionLedger } from "../io/openQuestions";
import type { ChatMessage } from "../types/agent";
import { toast, type ToastOptions } from "./ToastManager";
import { emit } from "../events";

/**
 * Which open questions this page has already put in front of the person, so
 * each gets one toast however many times the page goes live. A toast closed
 * unanswered stays closed: the question is still open, in the Inbox and its
 * thread, and a dropped connection is no reason to raise it again.
 */
export class QuestionNotices {
    private readonly _told = new Set<string>();

    /** Whether the question `id` is new to this page, and from now on it is not. */
    firstTime(id: string): boolean {
        const first = !this._told.has(id);
        this._told.add(id);
        return first;
    }

    /** Forget all but the questions in `open`: the rest are answered, and need no remembering. */
    keepOnly(open: readonly { id: string }[]): void {
        const still = new Set(open.map(msg => msg.id));
        [...this._told].filter(id => !still.has(id)).forEach(id => this._told.delete(id));
    }
}

/** The toast asking `msg`'s question, or null when `msg` asks nothing still open. */
export function questionToast(msg: ChatMessage): ToastOptions | null {
    const options = msg.options ?? [];
    if (!options.length || msg.answer !== undefined) {
        return null;
    }
    return {
        type: "chat",
        title: msg.from,
        message: msg.content,
        sticky: true,
        key: msg.id,
        actions: options.map((option, index) => ({
            label: option,
            onClick: () =>
                emit("af-answer-option", { id: msg.id, agentId: msg.agentId ?? "", option, index }),
        })),
    };
}

/**
 * Raises each question's toast, once, however it reached the page: live, or from
 * the ledger when the page goes live. None shows while the Inbox is open, where
 * the question is on screen already.
 */
export class QuestionToasts {
    private readonly _notices = new QuestionNotices();

    /** @param inboxOpen whether the Inbox is the view on screen. */
    constructor(private readonly inboxOpen: () => boolean) {}

    /** Whether `msg` asks something still open, its toast raised if it is new. */
    arrived(msg: ChatMessage): boolean {
        const asking = questionToast(msg);
        if (asking && this._notices.firstTime(msg.id) && !this.inboxOpen()) {
            toast.show(asking);
        }
        return asking !== null;
    }

    /** Take in a read of the ledger: what it lists as open, and what no longer is. */
    listed(ledger: QuestionLedger): void {
        // A failed read lists nothing, which says nothing about what is still open.
        if (ledger.listed) {
            this._notices.keepOnly(ledger.open);
        }
        ledger.open.forEach(msg => this.arrived(msg));
    }
}
