/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The wiring the view shows, and when it is read again.
 *
 * The view is handed what this holds and never asks the server itself. It is
 * read when the view opens, when something it shows may have changed --
 * agents came, went or moved, a model's pointer moved, an agent's questions
 * changed -- when asked to, every `REFRESH_MS` while the view is showing, and
 * when the page comes back from being hidden. Nothing is read while the view
 * is not showing.
 *
 * Only the newest request counts: an answer to one asked earlier is dropped,
 * so a slow reply never replaces a newer one. A failed read keeps the wiring
 * read before, said to be stale and why, rather than blanking it; with
 * nothing read yet it is an error. A `503` is no failure but the server
 * saying it has no registry to read, which is said as such.
 */
import { fetchWiring } from "../../io/wiringApi";
import type { Answer } from "../../io/api";
import type { Wiring } from "../../types/wiring";

/** How often the wiring is read while the view is showing. */
export const REFRESH_MS = 30_000;
/** How long a burst of changes is waited out before the wiring is read once. */
export const SETTLE_MS = 1_000;

/** Where the wiring stands. */
export type WiringStatus = "idle" | "loading" | "ready" | "unavailable" | "error";

/** What the view is handed. */
export interface WiringState {
    status: WiringStatus;
    /** The wiring last read; null before any was. */
    wiring: Wiring | null;
    /** Why the last read failed, or why there is nothing to read; "" when it did not. */
    error: string;
    /** When `wiring` was read, in milliseconds since the epoch; null before. */
    readAt: number | null;
    /** Whether a read is under way. */
    reading: boolean;
}

/** What the controller asks of the dashboard. */
export interface WiringDataHost {
    /** Whether the Wiring view is the one showing. */
    isShowing(): boolean;
}

/** How the controller reads the wiring; replaced in tests. */
export type WiringReader = () => Promise<Answer<Wiring>>;

/** The wiring, and the reads that keep it fresh. */
export class WiringData {
    private _state: WiringState = { status: "idle", wiring: null, error: "", readAt: null, reading: false };
    private readonly _listeners = new Set<(state: WiringState) => void>();
    private _asked = 0;
    private _timer: ReturnType<typeof setInterval> | null = null;
    private _settle: ReturnType<typeof setTimeout> | null = null;
    private readonly _onVisible = (): void => {
        if (!document.hidden && this._host.isShowing()) {
            void this.refresh();
        }
    };

    constructor(
        private readonly _host: WiringDataHost,
        private readonly _read: WiringReader = fetchWiring,
    ) {}

    /** What is held now. */
    get state(): WiringState {
        return this._state;
    }

    /** Be told what is held, now and on every change. Returns the way to stop. */
    subscribe(listener: (state: WiringState) => void): () => void {
        this._listeners.add(listener);
        listener(this._state);
        return () => this._listeners.delete(listener);
    }

    /** Begin the timer and listen for the page coming back; reads nothing yet. */
    start(): void {
        this.stop();
        this._timer = setInterval(() => {
            if (!document.hidden && this._host.isShowing()) {
                void this.refresh();
            }
        }, REFRESH_MS);
        document.addEventListener("visibilitychange", this._onVisible);
    }

    /** Stop every timer and listener, and drop any read under way. */
    stop(): void {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
        if (this._settle) {
            clearTimeout(this._settle);
            this._settle = null;
        }
        document.removeEventListener("visibilitychange", this._onVisible);
        this._asked += 1;
        if (this._state.reading) {
            this._set({ reading: false });
        }
    }

    /** The view opened: read now. */
    opened(): void {
        void this.refresh();
    }

    /** Something the wiring shows may have changed: read once the burst settles, if the view is showing. */
    changed(): void {
        if (!this._host.isShowing()) {
            return;
        }
        if (this._settle) {
            clearTimeout(this._settle);
        }
        this._settle = setTimeout(() => {
            this._settle = null;
            void this.refresh();
        }, SETTLE_MS);
    }

    /** Read the wiring now. */
    async refresh(): Promise<void> {
        const mine = ++this._asked;
        this._set({ reading: true, status: this._state.wiring ? this._state.status : "loading" });
        const answer = await this._read();
        if (mine !== this._asked) {
            return;
        }
        if (answer.ok) {
            this._set({
                status: "ready",
                wiring: answer.value,
                error: "",
                readAt: Date.now(),
                reading: false,
            });
        } else if (answer.status === 503) {
            this._set({
                status: "unavailable",
                wiring: null,
                error: answer.error,
                readAt: null,
                reading: false,
            });
        } else if (this._state.wiring) {
            this._set({ status: "ready", error: answer.error, reading: false });
        } else {
            this._set({ status: "error", error: answer.error, reading: false });
        }
    }

    private _set(change: Partial<WiringState>): void {
        this._state = { ...this._state, ...change };
        for (const listener of this._listeners) {
            listener(this._state);
        }
    }
}
