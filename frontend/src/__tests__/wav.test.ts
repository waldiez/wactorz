/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, afterEach } from "vitest";
import { STT_SAMPLE_RATE, downmix, encodeWav, toWav16k } from "../io/wav";

function text(view: DataView, offset: number, length: number): string {
    return String.fromCharCode(...Array.from({ length }, (_, i) => view.getUint8(offset + i)));
}

describe("encodeWav", () => {
    it("writes a 16-bit mono PCM WAV header the server recognises", () => {
        const view = new DataView(encodeWav(new Float32Array(160), 16_000));

        expect(text(view, 0, 4)).toBe("RIFF");
        expect(text(view, 8, 4)).toBe("WAVE");
        expect(view.getUint16(20, true)).toBe(1); // PCM
        expect(view.getUint16(22, true)).toBe(1); // mono
        expect(view.getUint32(24, true)).toBe(16_000);
        expect(view.getUint16(34, true)).toBe(16);
        expect(view.getUint32(40, true)).toBe(320);
        expect(view.byteLength).toBe(44 + 320);
    });

    it("scales samples to 16-bit and clips anything out of range", () => {
        const view = new DataView(encodeWav(new Float32Array([0, 1, -1, 2, -2]), 16_000));
        const sample = (i: number) => view.getInt16(44 + i * 2, true);

        expect([sample(0), sample(1), sample(2), sample(3), sample(4)]).toEqual([
            0, 32767, -32768, 32767, -32768,
        ]);
    });
});

describe("downmix", () => {
    it("averages the channels", () => {
        const left = new Float32Array([1, 0]);
        const right = new Float32Array([0, 0.5]);
        const audio = {
            length: 2,
            numberOfChannels: 2,
            getChannelData: (c: number) => (c === 0 ? left : right),
        } as unknown as AudioBuffer;

        expect(Array.from(downmix(audio))).toEqual([0.5, 0.25]);
    });
});

describe("toWav16k", () => {
    const original = (globalThis as { OfflineAudioContext?: unknown }).OfflineAudioContext;
    afterEach(() => {
        (globalThis as { OfflineAudioContext?: unknown }).OfflineAudioContext = original;
    });

    it("says so where the browser cannot decode audio", async () => {
        (globalThis as { OfflineAudioContext?: unknown }).OfflineAudioContext = undefined;
        await expect(toWav16k(new Blob(["x"]))).rejects.toThrow("cannot prepare audio");
    });

    it("decodes at 16 kHz and returns a WAV blob", async () => {
        const rates: number[] = [];
        class FakeOffline {
            constructor(_channels: number, _length: number, rate: number) {
                rates.push(rate);
            }
            async decodeAudioData() {
                return {
                    length: 3,
                    numberOfChannels: 1,
                    sampleRate: STT_SAMPLE_RATE,
                    getChannelData: () => new Float32Array([0, 0.5, -0.5]),
                };
            }
        }
        (globalThis as { OfflineAudioContext?: unknown }).OfflineAudioContext = FakeOffline;

        const wav = await toWav16k(new Blob(["webm"], { type: "audio/webm" }));

        expect(rates).toEqual([STT_SAMPLE_RATE]);
        expect(wav.type).toBe("audio/wav");
        expect(wav.size).toBe(44 + 6);
    });
});
