/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * When each remote node was last heard from, to tell a node coming online
 * from one carrying on.
 *
 * A node reports every few seconds. That it is still there is routine, and
 * shown with the other heartbeats; that it has just appeared, or come back
 * after going quiet, is news. The window is the caller's, so it can be the one
 * the overview uses to call a node offline.
 */
export class NodePresence {
    private readonly _lastHeard = new Map<string, number>();

    /** `staleMs`: how long a node may be silent and still count as online. */
    constructor(private readonly staleMs: number) {}

    /**
     * Record a report from `node` at `now`; whether it comes online with it —
     * the first report since the page opened, or one after `staleMs` of silence.
     */
    heard(node: string, now = Date.now()): boolean {
        const before = this._lastHeard.get(node);
        this._lastHeard.set(node, now);
        return before === undefined || now - before >= this.staleMs;
    }
}
