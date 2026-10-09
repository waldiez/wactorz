/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Speech recognition through the browser's own Web Speech API.
 *
 * Chrome streams the microphone to Google's recogniser, which is a transducer
 * and so returns hypotheses while speech is still arriving. Results marked
 * `isFinal: false` are revised as more context comes in; the final one arrives
 * when the service decides the utterance ended.
 *
 * Chromium only in practice (Chrome sends the audio to Google, Edge to
 * Microsoft), needs a network connection, and needs a secure context: the
 * dashboard opened as `localhost` or over HTTPS. Firefox has no usable version.
 */

/** Called with each hypothesis. `isFinal` marks text that will not be revised. */
export type TranscriptCallback = (text: string, isFinal: boolean) => void;

interface SpeechRecognitionResultItem {
    readonly transcript: string;
}

interface SpeechRecognitionResult {
    readonly isFinal: boolean;
    readonly length: number;
    [index: number]: SpeechRecognitionResultItem | undefined;
}

interface SpeechRecognitionResultList {
    readonly length: number;
    [index: number]: SpeechRecognitionResult | undefined;
}

interface SpeechRecognitionEventLike extends Event {
    readonly resultIndex: number;
    readonly results: SpeechRecognitionResultList;
}

interface SpeechRecognitionInstance extends EventTarget {
    continuous: boolean;
    interimResults: boolean;
    lang: string;
    onresult: ((e: SpeechRecognitionEventLike) => void) | null;
    onend: (() => void) | null;
    onerror: ((e: { error: string }) => void) | null;
    start(): void;
    stop(): void;
}

type SpeechRecognitionConstructor = new () => SpeechRecognitionInstance;

function constructor(): SpeechRecognitionConstructor | undefined {
    const w = window as unknown as {
        SpeechRecognition?: SpeechRecognitionConstructor;
        webkitSpeechRecognition?: SpeechRecognitionConstructor;
    };
    return w.SpeechRecognition ?? w.webkitSpeechRecognition;
}

export class WebSpeech {
    private recognition: SpeechRecognitionInstance | null = null;

    /** Whether this browser exposes the API at all. */
    static isSupported(): boolean {
        return typeof window !== "undefined" && constructor() !== undefined;
    }

    /** True from `start()` until the recogniser reports it has ended. */
    get listening(): boolean {
        return this.recognition !== null;
    }

    /** Begin listening. `onText` fires repeatedly, `onEnd` once when it stops. */
    start(
        onText: TranscriptCallback,
        onEnd: () => void,
        onError: (why: string) => void,
        lang?: string,
    ): void {
        const Recognition = constructor();
        if (!Recognition || this.recognition) {
            return;
        }
        const recognition = new Recognition();
        recognition.continuous = false;
        recognition.interimResults = true;
        recognition.lang = lang || navigator.language || "en-US";

        recognition.onresult = event => {
            // Only results from resultIndex onward are new; earlier ones have
            // already been reported and would otherwise be repeated.
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const result = event.results[i];
                const text = result?.[0]?.transcript;
                if (result && text) {
                    onText(text, result.isFinal);
                }
            }
        };
        recognition.onerror = e => onError(e.error);
        recognition.onend = () => {
            this.recognition = null;
            onEnd();
        };

        this.recognition = recognition;
        try {
            recognition.start();
        } catch (err) {
            // Thrown synchronously when the page is not a secure context or a
            // previous session has not finished releasing the microphone.
            this.recognition = null;
            onError(err instanceof Error ? err.message : String(err));
            onEnd();
        }
    }

    /** Ask it to stop; the final result still arrives before `onEnd`. */
    stop(): void {
        this.recognition?.stop();
    }
}
