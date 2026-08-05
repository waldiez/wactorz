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

/** Everything the stream UI needs from the chat controller. */
export interface StreamHost {
    /** The open view is "chat" (rows only render there). */
    isChatView(): boolean;
    /** The agent the user last sent to (fallback reply attribution). */
    lastSentTarget(): string;
    /** Would a message from `from` to `to` belong to the open thread? */
    belongsHere(from: string, to: string): boolean;
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

export class ChatStreamUI {
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

    /** Latch the reply target on the first chunk, then accumulate per agent. */
    onChunk(detail: { chunk: string; from: string }): void {
        const { chunk, from } = detail;
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
