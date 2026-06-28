/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { SpeechToText, STT_ENABLED } from "../io/SpeechToText";

describe("SpeechToText (feature flags & happy-dom defaults)", () => {
    it("exposes the STT_ENABLED feature constant as a boolean", () => {
        expect(typeof STT_ENABLED).toBe("boolean");
    });

    it("reports unsupported when MediaRecorder/getUserMedia are absent (happy-dom)", () => {
        expect(SpeechToText.isSupported()).toBe(false);
    });

    it("is not recording before any capture starts", () => {
        expect(new SpeechToText().recording).toBe(false);
    });

    it("stopAndTranscribe returns '' when nothing was recorded", async () => {
        const text = await new SpeechToText().stopAndTranscribe();
        expect(text).toBe("");
    });
});

describe("SpeechToText capture & transcription (mocked media APIs)", () => {
    const recorders: FakeMediaRecorder[] = [];
    const last = () => recorders[recorders.length - 1]!;
    let trackStop: ReturnType<typeof vi.fn>;

    class FakeMediaRecorder {
        state = "inactive";
        mimeType = "audio/webm";
        ondataavailable: ((e: { data: { size: number } }) => void) | null = null;
        onstop: (() => void) | null = null;
        constructor(public stream: unknown) {
            recorders.push(this);
        }
        start() {
            this.state = "recording";
        }
        stop() {
            this.state = "inactive";
            this.onstop?.();
        }
    }

    const origFetch = globalThis.fetch;
    const origMR = (globalThis as { MediaRecorder?: unknown }).MediaRecorder;
    const origMD = (navigator as { mediaDevices?: unknown }).mediaDevices;

    beforeEach(() => {
        recorders.length = 0;
        trackStop = vi.fn();
        (globalThis as { MediaRecorder?: unknown }).MediaRecorder = FakeMediaRecorder;
        (navigator as { mediaDevices?: unknown }).mediaDevices = {
            getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: trackStop }] })),
        };
    });

    afterEach(() => {
        globalThis.fetch = origFetch;
        (globalThis as { MediaRecorder?: unknown }).MediaRecorder = origMR;
        (navigator as { mediaDevices?: unknown }).mediaDevices = origMD;
        vi.restoreAllMocks();
    });

    it("isSupported is true once the media APIs are present", () => {
        expect(SpeechToText.isSupported()).toBe(true);
    });

    it("start() begins recording and is idempotent", async () => {
        const stt = new SpeechToText();
        await stt.start();
        expect(stt.recording).toBe(true);
        const first = last();
        await stt.start(); // already recording → no new recorder
        expect(last()).toBe(first);
    });

    it("collects non-empty data chunks and returns a blob on stop", async () => {
        const stt = new SpeechToText();
        await stt.start();
        last().ondataavailable!({ data: { size: 10 } });
        last().ondataavailable!({ data: { size: 0 } }); // ignored
        const blob = await stt.stop();
        expect(blob).toBeInstanceOf(Blob);
        expect(stt.recording).toBe(false);
        expect(trackStop).toHaveBeenCalled(); // mic released
    });

    it("stop() returns null when there is no recorder", async () => {
        expect(await new SpeechToText().stop()).toBeNull();
    });

    it("transcribe() POSTs the audio and returns the recognized text", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ text: "hello there" }),
        })) as unknown as typeof fetch;
        const stt = new SpeechToText("/ingress");
        const text = await stt.transcribe(new Blob(["x"]));
        expect(text).toBe("hello there");
        expect(fetch).toHaveBeenCalledWith("/ingress/api/stt", expect.objectContaining({ method: "POST" }));
    });

    it("transcribe() returns '' when the response has no text field", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({}),
        })) as unknown as typeof fetch;
        expect(await new SpeechToText().transcribe(new Blob(["x"]))).toBe("");
    });

    it("transcribe() throws on a non-OK response", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500 })) as unknown as typeof fetch;
        await expect(new SpeechToText().transcribe(new Blob(["x"]))).rejects.toThrow("STT failed (500)");
    });

    it("stopAndTranscribe() records, stops, and transcribes in one step", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ text: "done" }),
        })) as unknown as typeof fetch;
        const stt = new SpeechToText();
        await stt.start();
        last().ondataavailable!({ data: { size: 5 } });
        expect(await stt.stopAndTranscribe()).toBe("done");
    });

    it("cancel() aborts an in-progress recording and releases the mic", async () => {
        const stt = new SpeechToText();
        await stt.start();
        stt.cancel();
        expect(stt.recording).toBe(false);
        expect(trackStop).toHaveBeenCalled();
    });
});
