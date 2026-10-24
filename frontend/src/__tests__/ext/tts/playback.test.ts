/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { emit } from "../../../events";

/** A source that records what was done to it, and can be ended by the test. */
interface FakeSource {
    label: string;
    started: boolean;
    stopped: boolean;
    onended: (() => void) | null;
    buffer: unknown;
    connect: () => void;
    start: () => void;
    stop: () => void;
}

let sources: FakeSource[] = [];

/** Each decoded buffer carries the text it was made for, so a source can say which reply it is. */
class RecordingContext {
    state = "running";
    currentTime = 0;
    destination = {};
    createOscillator() {
        return { type: "sine", frequency: { value: 0 }, connect() {}, start() {}, stop() {} };
    }
    createGain() {
        return { gain: { value: 1, setTargetAtTime() {} }, connect() {} };
    }
    createBufferSource(): FakeSource {
        const source: FakeSource = {
            label: "",
            started: false,
            stopped: false,
            onended: null,
            buffer: null,
            connect() {},
            start() {
                source.started = true;
                source.label = String((source.buffer as { label: string }).label);
            },
            stop() {
                source.stopped = true;
            },
        };
        sources.push(source);
        return source;
    }
    decodeAudioData(buf: ArrayBuffer): Promise<{ label: string }> {
        return Promise.resolve({ label: new TextDecoder().decode(buf) });
    }
    resume() {
        return Promise.resolve();
    }
}

/** A /api/tts the test answers when it chooses, one reply per request, in order. */
function heldServer(): { answer: (index: number) => void; fail: (index: number) => void } {
    const pending: { text: string; resolve: (r: unknown) => void; reject: (e: unknown) => void }[] = [];
    vi.stubGlobal(
        "fetch",
        vi.fn(
            (_url: string, init: RequestInit) =>
                new Promise((resolve, reject) => {
                    pending.push({ text: JSON.parse(init.body as string).text, resolve, reject });
                }),
        ),
    );
    return {
        answer(index) {
            const req = pending[index]!;
            req.resolve({
                ok: true,
                status: 200,
                arrayBuffer: () => Promise.resolve(new TextEncoder().encode(req.text).buffer),
            });
        },
        fail(index) {
            pending[index]!.reject(new Error("network down"));
        },
    };
}

async function speaking() {
    localStorage.setItem("wactorz.tts", "1");
    localStorage.setItem("wactorz.beep", "0");
    const mod = await import("../../../ext/tts");
    return { mod, m: new mod.TTSManager() };
}

const settle = () => new Promise(r => setTimeout(r, 0));

describe("one reply heard at a time", () => {
    const ducking: string[] = [];
    const onStart = () => ducking.push("start");
    const onEnd = () => ducking.push("end");

    beforeEach(() => {
        localStorage.clear();
        vi.resetModules();
        vi.clearAllMocks();
        sources = [];
        ducking.length = 0;
        vi.stubGlobal("AudioContext", RecordingContext);
        document.addEventListener("tts-audio-start", onStart);
        document.addEventListener("tts-audio-end", onEnd);
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        document.removeEventListener("tts-audio-start", onStart);
        document.removeEventListener("tts-audio-end", onEnd);
    });

    it("stops the reply playing when the next one starts", async () => {
        const server = heldServer();
        const { m } = await speaking();

        m.notify("first");
        server.answer(0);
        await vi.waitFor(() => expect(sources[0]?.started).toBe(true));
        m.notify("second");
        server.answer(1);
        await vi.waitFor(() => expect(sources[1]?.started).toBe(true));

        // Otherwise the two talk over each other.
        expect(sources[0]!.stopped).toBe(true);
        expect(sources[1]!.label).toBe("second");
    });

    it("drops a reply whose audio arrives after a newer one was asked for", async () => {
        const server = heldServer();
        const { m } = await speaking();

        m.notify("slow");
        m.notify("fast");
        server.answer(1);
        await vi.waitFor(() => expect(sources).toHaveLength(1));
        server.answer(0);
        await settle();
        await settle();

        expect(sources.map(s => s.label)).toEqual(["fast"]);
    });

    it("drops a reply overtaken while its audio was being decoded", async () => {
        const decoding: (() => void)[] = [];
        class SlowDecoder extends RecordingContext {
            override decodeAudioData(buf: ArrayBuffer): Promise<{ label: string }> {
                const decoded = super.decodeAudioData(buf);
                return new Promise(resolve => decoding.push(() => resolve(decoded)));
            }
        }
        vi.stubGlobal("AudioContext", SlowDecoder);
        const server = heldServer();
        const { m } = await speaking();

        m.notify("decoding");
        server.answer(0);
        await vi.waitFor(() => expect(decoding).toHaveLength(1));
        m.stopSpeaking();
        decoding[0]!();
        await settle();

        expect(sources).toEqual([]);
    });

    it("does not fall back to the browser's voice for a reply already overtaken", async () => {
        const server = heldServer();
        const { m } = await speaking();
        const synth = (globalThis as any).speechSynthesis;

        m.notify("lost");
        m.notify("kept");
        server.fail(0);
        await settle();
        await settle();

        expect(synth.speak).not.toHaveBeenCalled();
    });

    it("turning speech off stops the server's voice and drops what is on its way", async () => {
        const server = heldServer();
        const { m } = await speaking();

        m.notify("playing");
        server.answer(0);
        await vi.waitFor(() => expect(sources[0]?.started).toBe(true));
        m.notify("coming");
        m.toggleTTS();
        server.answer(1);
        await settle();
        await settle();

        expect(sources[0]!.stopped).toBe(true);
        expect(sources.filter(s => s.started).map(s => s.label)).toEqual(["playing"]);
        expect((globalThis as any).speechSynthesis.cancel).toHaveBeenCalled();
    });

    it("keeps the ambient sound down across a change of reply, and lets it up once", async () => {
        const server = heldServer();
        const { m } = await speaking();

        m.notify("first");
        server.answer(0);
        await vi.waitFor(() => expect(sources[0]?.started).toBe(true));
        m.notify("second");
        server.answer(1);
        await vi.waitFor(() => expect(sources[1]?.started).toBe(true));
        // The stopped source reports its end after the next one has begun.
        sources[0]!.onended?.();
        expect(ducking).toEqual(["start"]);

        sources[1]!.onended?.();
        expect(ducking).toEqual(["start", "end"]);
    });

    it("lets the ambient sound up when stopped mid-reply, and says nothing when idle", async () => {
        const server = heldServer();
        const { m } = await speaking();

        m.stopSpeaking();
        expect(ducking).toEqual([]);

        m.notify("interrupted");
        server.answer(0);
        await vi.waitFor(() => expect(sources[0]?.started).toBe(true));
        m.stopSpeaking();
        sources[0]!.onended?.();

        expect(sources[0]!.stopped).toBe(true);
        expect(ducking).toEqual(["start", "end"]);
    });

    it("a stopped turn stops its voice, through the bus", async () => {
        const server = heldServer();
        const { mod } = await speaking();
        mod.register({ apiBase: "", available: false });
        // The shared instance is the one the page speaks through; it read "on" from storage.
        expect(mod.tts.ttsEnabled).toBe(true);

        mod.tts.notify("long answer");
        server.answer(0);
        await vi.waitFor(() => expect(sources[0]?.started).toBe(true));
        vi.mocked((globalThis as any).speechSynthesis.cancel).mockClear();
        emit("tts-stop");

        expect(sources[0]!.stopped).toBe(true);
        expect((globalThis as any).speechSynthesis.cancel).toHaveBeenCalled();
    });
});
