/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Recognition by the browser itself, for `WACTORZ_STT=browser`.
 *
 * No audio reaches this deployment on this branch -- but it does not follow that
 * it stays on the machine. Chrome sends what it hears to Google, and Safari to
 * Apple; the browser decides, and the page is not told. A deployment that must
 * keep speech inside its own network wants `server` with a self-hosted
 * recogniser, not this.
 *
 * Only Chromium implements it, and only where the page is trusted with a
 * microphone at all. Both are checked here rather than at the point of use,
 * because the alternative is a button that appears everywhere and fails on the
 * click in most places.
 */

interface SpeechResultItem {
    readonly transcript: string;
}

interface SpeechResult {
    readonly isFinal: boolean;
    readonly length: number;
    readonly [index: number]: SpeechResultItem | undefined;
}

interface SpeechResults {
    readonly length: number;
    readonly [index: number]: SpeechResult | undefined;
}

interface SpeechEvent extends Event {
    readonly resultIndex: number;
    readonly results: SpeechResults;
}

interface Recogniser extends EventTarget {
    continuous: boolean;
    interimResults: boolean;
    lang: string;
    onresult: ((event: SpeechEvent) => void) | null;
    onerror: ((event: Event & { error?: string }) => void) | null;
    onend: (() => void) | null;
    start(): void;
    stop(): void;
    abort(): void;
}

type RecogniserCtor = new () => Recogniser;

function ctor(): RecogniserCtor | undefined {
    const scope = window as unknown as {
        SpeechRecognition?: RecogniserCtor;
        webkitSpeechRecognition?: RecogniserCtor;
    };
    return scope.SpeechRecognition ?? scope.webkitSpeechRecognition;
}

/**
 * Whether this browser will actually recognise speech.
 *
 * The constructor being present is not enough: Chrome defines it on a page
 * served over plain HTTP and then refuses at `start()`, so checking only for it
 * offers a button that breaks when pressed.
 */
export function canRecognise(): boolean {
    if (typeof window === "undefined" || ctor() === undefined) {
        return false;
    }
    return window.isSecureContext === true;
}

/** What the composer does with a reading and with the turn ending. */
export interface WebSpeechHandlers {
    /** The reading so far, for the composer to show. Called many times. */
    onText: (text: string) => void;
    /** The turn is over. `reason` is absent when the person ended it. */
    onEnd: (reason?: string) => void;
}

/** One turn at the browser's own recogniser. */
export class WebSpeech {
    private _running: Recogniser | null = null;
    private _handlers: WebSpeechHandlers | null = null;
    private _before = "";

    /** Whether a turn is in progress. */
    get listening(): boolean {
        return this._running !== null;
    }

    /**
     * Start listening.
     *
     * `existing` is whatever the composer already held, so the reading is added
     * to a half-written message rather than replacing it.
     */
    start(existing: string, handlers: WebSpeechHandlers): void {
        if (this._running) {
            return;
        }
        const Recognition = ctor();
        if (!Recognition || !canRecognise()) {
            throw new Error("this browser cannot recognise speech");
        }
        const recogniser = new Recognition();
        // Readings as they come, and one turn rather than an open microphone:
        // this branch answers a button, not a room.
        recogniser.interimResults = true;
        recogniser.continuous = false;
        recogniser.lang = navigator.language || "en-US";

        this._before = existing.trim();
        this._handlers = handlers;
        this._running = recogniser;

        recogniser.onresult = event => this._heard(event);
        recogniser.onerror = event => this._finish(reasonFor(event.error));
        recogniser.onend = () => this._finish();
        recogniser.start();
    }

    /** End the turn the person started. */
    stop(): void {
        // Stopped rather than aborted: the last words are still being decided,
        // and aborting throws them away.
        this._running?.stop();
    }

    private _heard(event: SpeechEvent): void {
        const said: string[] = [];
        for (let i = 0; i < event.results.length; i++) {
            const result = event.results[i];
            const best = result?.[0];
            if (best) {
                said.push(best.transcript);
            }
        }
        const spoken = said.join(" ").replace(/\s+/g, " ").trim();
        this._handlers?.onText([this._before, spoken].filter(Boolean).join(" "));
    }

    private _finish(reason?: string): void {
        const handlers = this._handlers;
        this._running = null;
        this._handlers = null;
        handlers?.onEnd(reason);
    }
}

/** Why the browser stopped, in words worth showing someone. */
function reasonFor(error: string | undefined): string | undefined {
    if (error === "no-speech" || error === "aborted") {
        // Neither is a fault: nobody spoke, or the turn was ended deliberately.
        return undefined;
    }
    if (error === "not-allowed" || error === "service-not-allowed") {
        return "Microphone permission was denied.";
    }
    if (error === "network") {
        return "The browser could not reach its recognition service.";
    }
    return error ? `Recognition stopped: ${error}` : undefined;
}
