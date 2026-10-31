/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Replies the person answers by pressing a button: an agent's question, and the
 * server's "which one?" when a name they used fits more than one agent.
 *
 * A question is marked answered at the press, and again when an answer arrives
 * from another channel; the ledger read each time the page goes live closes or
 * drops what changed while it was away. A pick among addresses answers its
 * reply and sends the message that used the name again, to the address picked.
 */
import type { Attachment, ChatMessage } from "../../types/agent";
import type { ChatLog } from "../../chat/chatLog";
import type { Listeners } from "../../events";
import { toast } from "../ToastManager";
import { readdressed } from "./chatRouting";

/** What answering needs from the chat. */
export interface AnswersHost {
    /** The chat's messages. */
    readonly log: ChatLog;
    /** Redraw the open thread, if the chat is on screen and `msg` (any message, when
     *  omitted) is in it. */
    redrawThread(msg?: ChatMessage): void;
    /** Redraw the agent list: a question can open or lock an agent's row. */
    redrawSidebar(): void;
    /** Send `content`, with `attachments`, from the conversation open now. */
    send(content: string, attachments: Attachment[]): void;
}

export class ChatAnswers {
    constructor(private readonly host: AnswersHost) {}

    /** Listen, on `listeners`, for presses, answers from elsewhere and the ledger. */
    wire(listeners: Listeners): void {
        // Marked at the press, not at the broker's echo, so a second press cannot answer twice.
        // One listener for both: a press here and an answer from elsewhere read the same.
        listeners.on("af-answered", detail => this._markAnswered(detail.id, detail.option));
        listeners.on("af-answer-option", detail => this._markAnswered(detail.id, detail.option));
        listeners.on("af-pick-address", detail => this._pickAddress(detail.id, detail.address));
        // The ledger, each time the page goes live: a question asked while it was
        // away joins its thread, one answered meanwhile closes, one no longer
        // listed goes. Not a message arriving now, so no turn ends on it.
        listeners.on("af-questions-listed", detail => {
            const { changed, closed } = this.host.log.reconcile(detail.open, detail.answered);
            closed.forEach(id => toast.close(id));
            if (changed) {
                this.host.redrawThread();
                this.host.redrawSidebar();
            }
        });
    }

    /** Mark the question `id` answered with `option`, and redraw if it is on screen.
     *  The same for a press here and an answer from another channel: the buttons close either way. */
    private _markAnswered(id: string, option: string): void {
        const message = this.host.log.answer(id, option);
        if (!message) {
            return;
        }
        this.host.redrawThread(message);
        // The row of an agent that cannot be messaged locks again once nothing it asked is open.
        this.host.redrawSidebar();
    }

    /**
     * The person picked `address` among those the reply `id` offered: the reply
     * is answered, so its buttons close, and the message that used the name goes
     * again to that address, from the conversation open now, as though typed there.
     */
    private _pickAddress(id: string, address: string): void {
        const log = this.host.log;
        const offer = log.get(id);
        const choice = offer?.addressChoice;
        if (!choice || offer.answer !== undefined || !choice.addresses.includes(address)) {
            return;
        }
        log.update(id, m => ({ ...m, answer: address }));
        this.host.redrawThread();
        const sent = log.get(choice.retryOf);
        // Wiped since, there is nothing left to send again.
        if (sent) {
            this.host.send(readdressed(sent.content, address), sent.attachments ?? []);
        }
    }
}
