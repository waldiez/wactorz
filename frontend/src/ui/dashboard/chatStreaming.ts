/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat thread's streaming concern: the reply so far → live bubble →
 * committed message. The reply is put together once, where the stream arrives
 * (`IOManager`): each piece's event carries the text so far and the end's
 * carries all of it, kept here per agent so concurrent streams never merge.
 * Whether a turn is under way is the turn tracker's (`src/chat/turnTracker.ts`);
 * this only draws the row that says an agent is working.
 */
import type { ChatMessage } from "../../types/agent";
import { uid } from "../../ids";
import { AgentStreams } from "../../io/agentStreams";
import { stripActionBlocks } from "../../agents/actionBlocks";
import { renderAgentMarkdown } from "./chatThread";
import { el, live, testId } from "../dom";

/** Everything the stream UI needs from the chat controller. */
export interface StreamHost {
    /** The open view is "chat" (rows only render there). */
    isChatView(): boolean;
    /** The agent the user last sent to, by name and id (fallback reply attribution). */
    lastSentTarget(): { name: string; id: string };
    /** Is the thread `id` the one open now? */
    isOpenThread(id: string): boolean;
    /** The open thread element (null when the chat view isn't mounted). */
    thread(): HTMLElement | null;
    /** Keep the newest content in view as chunks land. */
    scrollThread(): void;
    /** Record a completed stream as a chat message. */
    commit(msg: ChatMessage): void;
}

/** The live streaming bubble (plain text while chunks accumulate; swapped for
 *  rendered markdown on stream-end). Returns the row and its text body. */
function buildStreamRow(from: string, initialText = ""): { row: HTMLElement; body: HTMLElement } {
    const row = testId(el("div", "af-chat-msg af-chat-msg-agent"), "agent-message");
    const body = testId(el("div", "af-chat-msg-bubble", initialText), "message-body");
    row.append(testId(el("div", "af-chat-msg-from", from), "message-from"), body);
    return { row, body };
}

/** The row shown between sending and the first token.
 *
 *  Kept separate from the streaming row rather than reusing it: this one has no
 *  buffer behind it, so anything that rebuilds from the buffers would drop it,
 *  and a turn that ends without a single token would leave it on screen.
 */
function buildWaitingRow(from: string): HTMLElement {
    const { row, body } = buildStreamRow(from);
    body.className = "af-chat-msg-bubble af-chat-waiting";
    // Announced once rather than per dot, and the dots themselves are decorative.
    testId(live(body, "status", `${from} is working`), "message-waiting");
    for (let i = 0; i < 3; i++) {
        const dot = el("span", "af-chat-waiting-dot");
        dot.setAttribute("aria-hidden", "true");
        body.appendChild(dot);
    }
    return row;
}

/** What of a reply still arriving the live row shows. */
function shownWhileStreaming(text: string): string {
    return stripActionBlocks(text, { streaming: true });
}

export class ChatStreamUI {
    /** Agent this turn is waiting on, or null when nothing is outstanding. */
    private _awaiting: string | null = null;
    /** The thread the turn waited on was sent to. */
    private _awaitingThread = "";
    private _waitingRow: HTMLElement | null = null;
    private _row: HTMLElement | null = null;
    private _body: HTMLElement | null = null;
    /** Agent whose stream the visible row currently shows. */
    private _rowFrom: string | null = null;
    private _streams = new AgentStreams();
    /** Which target each active stream is replying to, by name (latched at first chunk). */
    private _targets = new Map<string, string>();
    /** The thread each active stream is in, as its frames name it (latched at first chunk). */
    private _threads = new Map<string, string>();

    constructor(private host: StreamHost) {}

    /** Test-visible access to the keyed buffers. */
    get streams(): AgentStreams {
        return this._streams;
    }

    /** A turn is on the wire to the thread `threadId`: show that `from` is working
     *  until it answers, or the turn ends without an answer (the tracker says so). */
    awaiting(from: string, threadId: string): void {
        this._awaiting = from;
        this._awaitingThread = threadId;
        this._showWaiting();
    }

    /** Whether an outstanding turn belongs to the open thread, so the thread
     *  keeps room for its row instead of showing its empty state. */
    awaitingHere(): boolean {
        return this._awaiting !== null && this.host.isOpenThread(this._awaitingThread);
    }

    /** Whether a turn is outstanding at all, wherever it belongs. */
    get isAwaiting(): boolean {
        return this._awaiting !== null;
    }

    /**
     * End the wait, if this is the turn that ends it.
     *
     * Concurrent streams are a feature, so an event has to be attributed before
     * it counts: a chunk from one agent says nothing about whether another is
     * still working. An unattributed event ends whatever is outstanding, which
     * is the only useful reading of one.
     *
     * `from` omitted entirely — a dropped connection, a failed send — ends it
     * unconditionally, because in those cases no reply is coming for anyone.
     */
    endWait(from?: string | null): void {
        if (from === undefined || this._awaiting === null || from === null || from === this._awaiting) {
            this._awaiting = null;
            this._clearWaiting();
        }
    }

    private _showWaiting(): void {
        this._clearWaiting();
        if (!this._awaiting || !this.host.isChatView()) {
            return;
        }
        // The turn belongs to the conversation it was sent to. Without this the
        // row follows the reader: send to one agent, open another's thread, and
        // that one appears to be working.
        if (!this.host.isOpenThread(this._awaitingThread)) {
            return;
        }
        const thread = this.host.thread();
        if (!thread) {
            return;
        }
        this._waitingRow = buildWaitingRow(this._awaiting);
        thread.appendChild(this._waitingRow);
        this.host.scrollThread();
    }

    private _clearWaiting(): void {
        this._waitingRow?.remove();
        this._waitingRow = null;
    }

    /** Rebuild the waiting row after a thread re-render wiped it. */
    reattachWaiting(): void {
        this._showWaiting();
    }

    /** Latch the reply target on the first chunk, then keep the reply so far. */
    onChunk(detail: { chunk: string; from: string; agentId: string; text: string }): void {
        const { from, text: soFar } = detail;
        // The first token answers the question the row was asking -- but only
        // for the agent that sent it.
        this.endWait(from);
        if (!this._streams.has(from)) {
            const sent = this.host.lastSentTarget();
            this._targets.set(from, sent.name);
            this._threads.set(from, detail.agentId || sent.id);
        }
        this._streams.set(from, soFar);
        if (!this.host.isChatView()) {
            return;
        }
        const active = this._streams.activeFrom;
        if (!active) {
            return;
        }
        if (!this._row || this._rowFrom !== active) {
            this._ensureRow();
        }
        if (this._body) {
            this._body.textContent = shownWhileStreaming(this._streams.text(active));
        }
        this.host.scrollThread();
    }

    /** Commit the ended reply as a message; swap its row for rendered markdown
     *  (a concurrent stream's row stays put). */
    onEnd(detail: { text: string; from: string; agentId: string }): void {
        const { from } = detail;
        // Ends the wait on every ending, including one that produced no tokens
        // at all -- an error or a stop -- which would otherwise leave the row
        // waiting for something that is no longer coming.
        this.endWait(from);
        this._streams.take(from);
        const shown = stripActionBlocks(detail.text);
        const text = shown.trim() ? shown : "";
        if (text) {
            const sent = this.host.lastSentTarget();
            this.host.commit({
                id: uid("stream"),
                from,
                to: this._targets.get(from) ?? sent.name,
                threadId: this._threads.get(from) || detail.agentId || sent.id,
                content: text,
                timestampMs: Date.now(),
            });
        }
        this._targets.delete(from);
        this._threads.delete(from);
        if (this._body && this._rowFrom === from) {
            if (text) {
                this._body.textContent = "";
                this._body.appendChild(renderAgentMarkdown(text));
            } else {
                // A reply that was nothing but action blocks leaves no message,
                // so its live row would otherwise stay as an empty bubble.
                this._row?.remove();
            }
            this._row = this._body = this._rowFrom = null;
        }
    }

    /** The active stream's accumulated text if it belongs to the open thread
     *  (drives the empty-state decision and row reattach), else null. */
    streamHereText(): string | null {
        const active = this._streams.activeFrom;
        if (!active) {
            return null;
        }
        const text = shownWhileStreaming(this._streams.text(active));
        if (!text) {
            return null;
        }
        const thread = this._threads.get(active) ?? this.host.lastSentTarget().id;
        return this.host.isOpenThread(thread) ? text : null;
    }

    /** Rebuild the live row inside `thread` (after a full thread re-render). */
    reattachRow(thread: HTMLElement): void {
        const active = this._streams.activeFrom;
        if (active) {
            this._buildRow(thread, active, shownWhileStreaming(this._streams.text(active)));
        }
    }

    /** A thread wipe detached any live bubble; drop the stale DOM refs. */
    dropRefs(): void {
        this._row = this._body = this._rowFrom = null;
        // The wipe took the waiting row with it; the turn it stands for is still
        // outstanding, so only the reference goes.
        this._waitingRow = null;
    }

    private _ensureRow(): void {
        const thread = this.host.thread();
        const active = this._streams.activeFrom;
        if (!thread || !active) {
            return;
        }
        this._buildRow(thread, active);
    }

    private _buildRow(thread: HTMLElement, from: string, initialText = ""): void {
        const { row, body } = buildStreamRow(from, initialText);
        thread.appendChild(row);
        this._row = row;
        this._body = body;
        this._rowFrom = from;
    }
}
