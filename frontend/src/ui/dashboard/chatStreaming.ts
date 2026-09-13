/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat thread's streaming concern: per-agent accumulation → live bubble →
 * committed message. Extracted from DashboardChat (which sits at the lint
 * size ceiling); buffers are keyed per agent so concurrent streams never merge.
 */
import type { ChatMessage } from "../../types/agent";
import { uid } from "../../ids";
import { AgentStreams } from "../../io/agentStreams";
import { MAIN_AGENT } from "../../agents/naming";
import { renderAgentMarkdown } from "./chatThread";
import { TURN_IDLE_TIMEOUT_MS } from "./chatIobar";

/** Everything the stream UI needs from the chat controller. */
export interface StreamHost {
    /** The open view is "chat" (rows only render there). */
    isChatView(): boolean;
    /** The agent the user last sent to (fallback reply attribution). */
    lastSentTarget(): string;
    /** Would a message from `from` to `to` belong to the open thread? */
    belongsHere(from: string, to: string): boolean;
    /** Is `name` the agent whose thread is currently open? */
    isOpenThread(name: string): boolean;
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
    const row = document.createElement("div");
    row.className = "af-chat-msg af-chat-msg-agent";
    const fromEl = document.createElement("div");
    fromEl.className = "af-chat-msg-from";
    fromEl.textContent = from;
    const body = document.createElement("div");
    body.className = "af-chat-msg-bubble";
    body.textContent = initialText;
    row.append(fromEl, body);
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
    body.setAttribute("role", "status");
    body.setAttribute("aria-label", `${from} is working`);
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement("span");
        dot.className = "af-chat-waiting-dot";
        dot.setAttribute("aria-hidden", "true");
        body.appendChild(dot);
    }
    return row;
}

export class ChatStreamUI {
    /** Agent this turn is waiting on, or null when nothing is outstanding. */
    private _awaiting: string | null = null;
    private _waitingRow: HTMLElement | null = null;
    private _waitTimer: ReturnType<typeof setTimeout> | undefined;
    private _row: HTMLElement | null = null;
    private _body: HTMLElement | null = null;
    /** Agent whose stream the visible row currently shows. */
    private _rowFrom: string | null = null;
    private _streams = new AgentStreams();
    /** Which target each active stream is replying to (latched at first chunk). */
    private _targets = new Map<string, string>();

    constructor(private host: StreamHost) {}

    /** Test-visible access to the keyed buffers. */
    get streams(): AgentStreams {
        return this._streams;
    }

    /** A turn is on the wire: show that `from` is working until it answers.
     *
     *  Bounded, because none of the endings arrive at all if the backend dies
     *  mid-turn -- the row would then wait for the life of the page. The limit
     *  is the same one the composer uses to release its own buttons, so the two
     *  give up together rather than leaving one of them stranded.
     */
    awaiting(from: string): void {
        this._awaiting = from;
        this._showWaiting();
        clearTimeout(this._waitTimer);
        this._waitTimer = setTimeout(() => this.endWait(), TURN_IDLE_TIMEOUT_MS);
    }

    /** Whether an outstanding turn belongs to the open thread, so the thread
     *  keeps room for its row instead of showing its empty state. */
    awaitingHere(): boolean {
        return this._awaiting !== null && this.host.isOpenThread(this._awaiting);
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
            clearTimeout(this._waitTimer);
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
        if (!this.host.isOpenThread(this._awaiting)) {
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

    /** Latch the reply target on the first chunk, then accumulate per agent. */
    onChunk(detail: { chunk: string; from: string }): void {
        const { chunk, from } = detail;
        // The first token answers the question the row was asking -- but only
        // for the agent that sent it.
        this.endWait(from);
        if (!this._streams.has(from)) {
            this._targets.set(from, this.host.lastSentTarget());
        }
        this._streams.append(from, chunk);
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
            this._body.textContent = this._streams.text(active);
        }
        this.host.scrollThread();
    }

    /** Commit the ended stream's buffer as a message; swap its row for
     *  rendered markdown (a concurrent stream's row stays put). */
    onEnd(detail: { text: string | null; from: string } | null): void {
        const from = detail?.from ?? this._streams.activeFrom ?? MAIN_AGENT;
        // Ends the wait on every ending, including one that produced no tokens
        // at all -- an error or a stop -- which would otherwise leave the row
        // waiting for something that is no longer coming.
        this.endWait(detail?.from ?? null);
        const text = this._streams.take(from);
        if (text) {
            this.host.commit({
                id: uid("stream"),
                from,
                to: this._targets.get(from) ?? this.host.lastSentTarget(),
                content: text,
                timestampMs: Date.now(),
            });
        }
        this._targets.delete(from);
        if (this._body && text && this._rowFrom === from) {
            this._body.textContent = "";
            this._body.appendChild(renderAgentMarkdown(text));
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
        const text = this._streams.text(active);
        if (!text) {
            return null;
        }
        const to = this._targets.get(active) ?? this.host.lastSentTarget();
        return this.host.belongsHere(active, to) ? text : null;
    }

    /** Rebuild the live row inside `thread` (after a full thread re-render). */
    reattachRow(thread: HTMLElement): void {
        const active = this._streams.activeFrom;
        if (active) {
            this._buildRow(thread, active, this._streams.text(active));
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
