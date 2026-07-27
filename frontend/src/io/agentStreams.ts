/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Per-agent stream accumulation.
 *
 * Stream chunks arrive tagged by agent, but a single shared buffer lets two
 * agents streaming concurrently merge into one bubble attributed to the first.
 * Buffers are keyed by agent so chunks never merge; each is individually
 * capped so a runaway stream can't grow without bound. `activeFrom` (the
 * first-started stream) drives the single visible streaming row.
 */
import { MAX_STREAM_CHARS } from "./streamCap";

export class AgentStreams {
    private _buffers = new Map<string, string>();

    /** Append a chunk to `from`'s buffer (created on the first chunk).
     *  Strictly capped: even a single oversized chunk is truncated at the cap. */
    append(from: string, chunk: string): void {
        const cur = this._buffers.get(from) ?? "";
        this._buffers.set(from, (cur + chunk).slice(0, MAX_STREAM_CHARS));
    }

    /** True while `from` has an unfinished stream. */
    has(from: string): boolean {
        return this._buffers.has(from);
    }

    /** The accumulated text for `from` ("" when none). */
    text(from: string): string {
        return this._buffers.get(from) ?? "";
    }

    /** The agent whose stream started first, if any. */
    get activeFrom(): string | null {
        return this._buffers.keys().next().value ?? null;
    }

    /** Remove and return `from`'s buffer (on stream end). */
    take(from: string): string {
        const text = this.text(from);
        this._buffers.delete(from);
        return text;
    }

    /** Drop every buffer (a full chat wipe), abandoning any stream in flight. */
    clear(): void {
        this._buffers.clear();
    }
}
