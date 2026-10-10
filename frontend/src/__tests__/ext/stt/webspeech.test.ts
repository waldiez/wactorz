/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, afterEach } from "vitest";

import { WebSpeech, canRecognise } from "../../../ext/stt/webSpeech";

/** A recogniser of the shape Chromium exposes, driven by the test. */
function installRecogniser(secure = true): {
    say: (text: string, final?: boolean) => void;
    fail: (error: string) => void;
    finish: () => void;
    started: () => number;
    stopped: () => number;
} {
    const calls = { start: 0, stop: 0 };
    const made: { it: any } = { it: null };

    class Fake {
        continuous = false;
        interimResults = false;
        lang = "";
        onresult: ((e: any) => void) | null = null;
        onerror: ((e: any) => void) | null = null;
        onend: (() => void) | null = null;
        constructor() {
            made.it = this;
        }
        start(): void {
            calls.start += 1;
        }
        stop(): void {
            calls.stop += 1;
        }
        abort(): void {}
        addEventListener(): void {}
        removeEventListener(): void {}
        dispatchEvent(): boolean {
            return true;
        }
    }

    Reflect.set(window, "webkitSpeechRecognition", Fake);
    Reflect.set(window, "isSecureContext", secure);

    return {
        say: (text, final = false) =>
            made.it?.onresult?.({
                resultIndex: 0,
                results: { length: 1, 0: { isFinal: final, length: 1, 0: { transcript: text } } },
            }),
        fail: error => made.it?.onerror?.({ error }),
        finish: () => made.it?.onend?.(),
        started: () => calls.start,
        stopped: () => calls.stop,
    };
}

describe("whether this browser recognises speech", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
        Reflect.deleteProperty(window, "SpeechRecognition");
    });

    it("needs the browser to implement it", () => {
        Reflect.set(window, "isSecureContext", true);
        expect(canRecognise()).toBe(false);
    });

    it("needs a page the browser trusts with a microphone", () => {
        installRecogniser(false);

        // Chrome defines the constructor on plain HTTP and then refuses at
        // start(), so checking only for it offers a button that breaks.
        expect(canRecognise()).toBe(false);
    });

    it("is offered where both are true", () => {
        installRecogniser(true);
        expect(canRecognise()).toBe(true);
    });
});

describe("a turn at the browser's own recogniser", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
        Reflect.deleteProperty(window, "SpeechRecognition");
    });

    it("shows words on top of what the composer already held", () => {
        const speech = installRecogniser();
        const seen: string[] = [];
        const ear = new WebSpeech();

        ear.start("note:", { onText: t => seen.push(t), onEnd: () => {} });
        speech.say("buy milk");

        expect(seen.at(-1)).toBe("note: buy milk");
    });

    it("replaces the reading rather than repeating it", () => {
        const speech = installRecogniser();
        const seen: string[] = [];
        const ear = new WebSpeech();

        ear.start("", { onText: t => seen.push(t), onEnd: () => {} });
        speech.say("hello th");
        speech.say("hello there");

        expect(seen.at(-1)).toBe("hello there");
    });

    it("asks for readings as they come, for one turn", () => {
        installRecogniser();
        const ear = new WebSpeech();

        ear.start("", { onText: () => {}, onEnd: () => {} });

        const made = Reflect.get(window, "webkitSpeechRecognition");
        expect(made).toBeDefined();
        expect(ear.listening).toBe(true);
    });

    it("a second start while listening changes nothing", () => {
        const speech = installRecogniser();
        const ear = new WebSpeech();

        ear.start("", { onText: () => {}, onEnd: () => {} });
        ear.start("", { onText: () => {}, onEnd: () => {} });

        expect(speech.started()).toBe(1);
    });

    it("stops rather than aborts, so the last words still arrive", () => {
        const speech = installRecogniser();
        const ear = new WebSpeech();
        ear.start("", { onText: () => {}, onEnd: () => {} });

        ear.stop();

        expect(speech.stopped()).toBe(1);
    });

    it("ends the turn when the browser does", () => {
        const speech = installRecogniser();
        const ends: (string | undefined)[] = [];
        const ear = new WebSpeech();
        ear.start("", { onText: () => {}, onEnd: r => ends.push(r) });

        speech.finish();

        expect(ends).toEqual([undefined]);
        expect(ear.listening).toBe(false);
    });

    it("says why when the browser refuses", () => {
        const speech = installRecogniser();
        const ends: (string | undefined)[] = [];
        const ear = new WebSpeech();
        ear.start("", { onText: () => {}, onEnd: r => ends.push(r) });

        speech.fail("not-allowed");

        expect(ends[0]).toContain("permission");
    });

    it("treats silence and a deliberate stop as nothing worth reporting", () => {
        for (const error of ["no-speech", "aborted"]) {
            const speech = installRecogniser();
            const ends: (string | undefined)[] = [];
            const ear = new WebSpeech();
            ear.start("", { onText: () => {}, onEnd: r => ends.push(r) });

            speech.fail(error);

            expect(ends).toEqual([undefined]);
        }
    });

    it("refuses to start where the browser cannot recognise", () => {
        installRecogniser(false);

        expect(() => new WebSpeech().start("", { onText: () => {}, onEnd: () => {} })).toThrow(
            /cannot recognise/,
        );
    });
});

describe("what the browser's refusals are called", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
    });

    it("names a network failure as one", () => {
        const speech = installRecogniser();
        const ends: (string | undefined)[] = [];
        const ear = new WebSpeech();
        ear.start("", { onText: () => {}, onEnd: r => ends.push(r) });

        // Chrome recognises by sending audio away, so it can fail for want of a
        // network where a local recogniser would not.
        speech.fail("network");

        expect(ends[0]).toContain("recognition service");
    });

    it("passes an unfamiliar one through rather than swallowing it", () => {
        const speech = installRecogniser();
        const ends: (string | undefined)[] = [];
        const ear = new WebSpeech();
        ear.start("", { onText: () => {}, onEnd: r => ends.push(r) });

        speech.fail("audio-capture");

        expect(ends[0]).toContain("audio-capture");
    });
});
