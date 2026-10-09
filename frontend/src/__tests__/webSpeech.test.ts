/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { WebSpeech } from "../io/WebSpeech";

/** The part of the browser's SpeechRecognition the wrapper drives. */
class FakeRecognition {
    static last: FakeRecognition | null = null;
    static throwOnStart = false;
    continuous = true;
    interimResults = false;
    lang = "";
    started = false;
    onresult: ((e: unknown) => void) | null = null;
    onend: (() => void) | null = null;
    onerror: ((e: { error: string }) => void) | null = null;
    constructor() {
        FakeRecognition.last = this;
    }
    start() {
        if (FakeRecognition.throwOnStart) {
            throw new Error("not a secure context");
        }
        this.started = true;
    }
    stop() {
        this.onend?.();
    }
    /** Deliver results as the browser does: a list, and where the new ones begin. */
    emit(resultIndex: number, results: { text: string; isFinal: boolean }[]) {
        this.onresult?.({
            resultIndex,
            results: results.map(r => Object.assign([{ transcript: r.text }], { isFinal: r.isFinal })),
        });
    }
}

function install(name: "SpeechRecognition" | "webkitSpeechRecognition") {
    (window as unknown as Record<string, unknown>)[name] = FakeRecognition;
}

afterEach(() => {
    delete (window as unknown as Record<string, unknown>).SpeechRecognition;
    delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    FakeRecognition.last = null;
    FakeRecognition.throwOnStart = false;
});

describe("WebSpeech", () => {
    it("is unsupported where the browser has no recogniser", () => {
        expect(WebSpeech.isSupported()).toBe(false);
    });

    it("finds Chrome's prefixed recogniser", () => {
        install("webkitSpeechRecognition");
        expect(WebSpeech.isSupported()).toBe(true);
    });

    it("listens for one utterance with live hypotheses", () => {
        install("SpeechRecognition");
        const speech = new WebSpeech();
        speech.start(vi.fn(), vi.fn(), vi.fn(), "el-GR");

        const recognition = FakeRecognition.last!;
        expect(recognition.started).toBe(true);
        expect(recognition.continuous).toBe(false);
        expect(recognition.interimResults).toBe(true);
        expect(recognition.lang).toBe("el-GR");
        expect(speech.listening).toBe(true);
    });

    it("reports only the results that are new", () => {
        install("SpeechRecognition");
        const heard: [string, boolean][] = [];
        new WebSpeech().start((text, isFinal) => heard.push([text, isFinal]), vi.fn(), vi.fn());

        FakeRecognition.last!.emit(1, [
            { text: "already reported", isFinal: true },
            { text: "hello", isFinal: false },
        ]);

        expect(heard).toEqual([["hello", false]]);
    });

    it("ends once, after which it can listen again", () => {
        install("SpeechRecognition");
        const speech = new WebSpeech();
        const ended = vi.fn();
        speech.start(vi.fn(), ended, vi.fn());

        speech.stop();

        expect(ended).toHaveBeenCalledTimes(1);
        expect(speech.listening).toBe(false);
    });

    it("passes the browser's error code on", () => {
        install("SpeechRecognition");
        const failed = vi.fn();
        new WebSpeech().start(vi.fn(), vi.fn(), failed);

        FakeRecognition.last!.onerror?.({ error: "not-allowed" });

        expect(failed).toHaveBeenCalledWith("not-allowed");
    });

    it("reports a recogniser that refuses to start, and ends cleanly", () => {
        install("SpeechRecognition");
        FakeRecognition.throwOnStart = true;
        const speech = new WebSpeech();
        const failed = vi.fn();
        const ended = vi.fn();

        speech.start(vi.fn(), ended, failed);

        expect(failed).toHaveBeenCalledWith("not a secure context");
        expect(ended).toHaveBeenCalledTimes(1);
        expect(speech.listening).toBe(false);
    });
});
