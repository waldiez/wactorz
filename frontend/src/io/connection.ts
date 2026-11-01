/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Whether the page is live: the /ws is open AND the server's link to the broker
 * is up. Both are required: an open /ws with a dead broker means no events are
 * actually flowing.
 */
import { emit } from "../events";

/** Follows the socket and the broker, and says when the two together change. */
export class Connection {
    private _socketOpen = false;
    private _brokerUp = false;
    private _live = false;

    /** @param wentLive called each time the page goes live, once it has said so. */
    constructor(private readonly wentLive: () => void) {}

    /** Whether events are flowing. */
    get live(): boolean {
        return this._live;
    }

    /** The /ws opened or closed. */
    socket(open: boolean): void {
        this._socketOpen = open;
        emit("af-socket", { open });
        this._recompute();
    }

    /** The server's link to the broker went up or down. */
    broker(up: boolean): void {
        this._brokerUp = up;
        this._recompute();
    }

    private _recompute(): void {
        const live = this._socketOpen && this._brokerUp;
        if (live === this._live) {
            return;
        }
        this._live = live;
        emit("af-connection-status", { status: live ? "live" : "disconnected" });
        if (live) {
            this.wentLive();
        }
    }
}
