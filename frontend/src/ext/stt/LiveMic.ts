/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * One turn at the microphone, from the first frame to the settled text.
 *
 * Three pieces have to move together: the microphone produces frames, the socket
 * carries them, and the readings that come back replace each other rather than
 * accumulate. Holding them here keeps the composer's mic button to two calls,
 * and keeps the rule that the tail is sent before the stop out of the view.
 *
 * The turn ends by itself if the socket drops. A dropped frame is the signal --
 * the server's session dies with the connection, so audio sent after a reconnect
 * goes nowhere and words would simply stop appearing with the button still lit.
 *
 * Ending it is not instant. The last words leave as audio, and the reading for
 * them arrives afterwards, so the turn drains rather than closing on the click:
 * the microphone shuts immediately, and the text stays connected until the
 * recogniser has settled the final segment or stopped answering.
 */

import { LiveCapture } from "./liveCapture";
import { Transcript } from "./transcript";

/** How long to wait for the reading of the last words before giving up on it. */
const DRAIN_TIMEOUT_MS = 4000;

/** The part of the dashboard socket a turn at the microphone needs. */
export interface LiveSocket {
    startListening(): boolean;
    stopListening(): boolean;
    sendAudio(frame: ArrayBuffer): boolean;
    onTranscript(fn: (text: string, segment: number, final: boolean) => void): void;
    onTranscriptError(fn: (message: string) => void): void;
}

/** What the composer does with a turn's text and its ending. */
export interface LiveMicHandlers {
    /** The reading so far, for the composer to show. Called many times. */
    onText: (text: string) => void;
    /** The turn is over. `reason` is absent when the person ended it. */
    onEnd: (reason?: string) => void;
}

export class LiveMic {
    private _capture = new LiveCapture();
    private _transcript = new Transcript();
    private _handlers: LiveMicHandlers | null = null;
    private _draining: ReturnType<typeof setTimeout> | null = null;

    constructor(private socket: LiveSocket) {
        this.socket.onTranscript((text, segment, final) => {
            if (!this._handlers) {
                return;
            }
            this._transcript.hear(text, segment);
            this._handlers.onText(this._transcript.text);
            if (final && this._draining) {
                this._finish();
            }
        });
        this.socket.onTranscriptError(message => this._finish(message));
    }

    /**
     * Whether the microphone is open.
     *
     * False as soon as the person ends the turn, even though the last reading
     * has not arrived: nothing is being recorded, which is what the button in
     * the composer is reporting.
     */
    get listening(): boolean {
        return this._handlers !== null && this._draining === null;
    }

    /**
     * Open the microphone for one turn.
     *
     * `existing` is whatever the composer already held, so the reading is added
     * to a half-written message rather than replacing it.
     */
    async start(existing: string, handlers: LiveMicHandlers): Promise<void> {
        if (this._draining) {
            // Speaking again before the last reading arrived. Waiting for it now
            // would leave the button dead to the touch, so that turn ends here.
            this._finish();
        }
        if (this._handlers) {
            return;
        }
        if (!this.socket.startListening()) {
            throw new Error("not connected");
        }
        this._transcript.clear();
        this._transcript.start(existing);
        this._handlers = handlers;
        try {
            await this._capture.start(frame => this._send(frame));
        } catch (error) {
            this._handlers = null;
            this.socket.stopListening();
            throw error;
        }
        if (this._draining || this._handlers !== handlers) {
            // Ended while the permission prompt was open. Nothing was recorded,
            // so there is no reading on its way to wait for.
            this._finish();
            return;
        }
    }

    /**
     * Take what the composer now holds as the truth.
     *
     * For when the person types while the microphone is open: their edit would
     * otherwise be overwritten by the next reading.
     */
    rebase(current: string): void {
        if (!this._handlers) {
            return;
        }
        this._transcript.rebase(current);
    }

    /**
     * End the turn the person started.
     *
     * The reading of the last words is still to come, so the turn is not over
     * here: the microphone closes, and `onText` keeps running until the
     * recogniser settles that segment or the wait runs out.
     */
    stop(): void {
        if (!this._handlers || this._draining) {
            return;
        }
        // The tail goes before the stop: after it the server treats the turn as
        // closed, and the last fraction of a second would be dropped.
        for (const frame of this._capture.stop()) {
            if (!this._deliver(frame)) {
                // Waiting on a reading that cannot arrive would sit out the
                // whole drain and then end as though nothing had gone wrong.
                this._finish("connection lost");
                return;
            }
        }
        if (!this.socket.stopListening()) {
            this._finish("connection lost");
            return;
        }
        // Kept rather than cleared here: clearing now would drop the very words
        // the tail was sent to have recognised.
        this._draining = setTimeout(() => this._finish(), DRAIN_TIMEOUT_MS);
    }

    private _send(frame: ArrayBuffer): void {
        if (!this._deliver(frame)) {
            this._finish("connection lost");
        }
    }

    /** Send one frame, treating a throw as the refusal it is. */
    private _deliver(frame: ArrayBuffer): boolean {
        // Called from the audio callback, where nothing is waiting to catch a
        // throw: a socket that fails on send would leave the microphone open.
        try {
            return this.socket.sendAudio(frame);
        } catch {
            return false;
        }
    }

    private _finish(reason?: string): void {
        const handlers = this._handlers;
        if (!handlers) {
            return;
        }
        if (this._draining) {
            clearTimeout(this._draining);
            this._draining = null;
        }
        this._handlers = null;
        this._capture.stop();
        handlers.onEnd(reason);
    }
}

// The composer reads this the way it reads the other capabilities: at click
// time, from module state. The socket is built once for the page, several
// layers away from the button, and threading it down through them would make
// every layer in between carry a dependency none of them use.
let attached: LiveMic | null = null;

/** Give the microphone the page's socket. */
export function attachLiveSocket(socket: LiveSocket): void {
    attached = new LiveMic(socket);
}

/** The page's microphone, once a socket has been attached. */
export function liveMic(): LiveMic | null {
    return attached;
}
