/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Turning what the browser records into what the recogniser reads.
 *
 * The DOM these tests run in has no AudioContext, so the decode is exercised
 * against a stand-in. That the stand-in suffices is the point of keeping the
 * arithmetic separate from the decoding.
 */
import { describe, it, expect, afterEach } from "vitest";

import { encodeWav, resample, toMono, toWav, TARGET_RATE } from "../../../ext/stt";

/** Read a little-endian field out of an encoded clip. */
function field(buffer: ArrayBuffer, offset: number, bytes: 2 | 4): number {
    const view = new DataView(buffer);
    return bytes === 2 ? view.getUint16(offset, true) : view.getUint32(offset, true);
}

function text(buffer: ArrayBuffer, offset: number, length: number): string {
    return String.fromCharCode(...new Uint8Array(buffer, offset, length));
}

describe("mixing down to mono", () => {
    it("averages the channels", () => {
        const left = new Float32Array([1, 0, -1]);
        const right = new Float32Array([0, 0, 1]);

        expect(Array.from(toMono([left, right]))).toEqual([0.5, 0, 0]);
    });

    it("passes a mono track through untouched", () => {
        const only = new Float32Array([0.25, -0.25]);

        expect(toMono([only])).toBe(only);
    });

    it("survives being handed nothing", () => {
        expect(toMono([]).length).toBe(0);
    });
});

describe("resampling", () => {
    it("returns the samples unchanged when the rate already matches", () => {
        const samples = new Float32Array([0.1, 0.2]);

        expect(resample(samples, 16000, 16000)).toBe(samples);
    });

    it("shortens a downsampled run in proportion", () => {
        const samples = new Float32Array(480); // 10ms at 48k

        expect(resample(samples, 48000, 16000).length).toBe(160); // 10ms at 16k
    });

    it("interpolates rather than dropping samples", () => {
        const samples = new Float32Array([0, 1, 2, 3]);

        // Halving the rate reads at 0 and 2, which are exact samples; the point
        // of interpolation is that a fractional position lands between them.
        const halved = resample(samples, 2, 1);
        expect(Array.from(halved)).toEqual([0, 2]);
    });
});

describe("encoding a clip", () => {
    it("writes a WAV header the recogniser can read", () => {
        const buffer = encodeWav(new Float32Array([0, 0]), TARGET_RATE);

        expect(text(buffer, 0, 4)).toBe("RIFF");
        expect(text(buffer, 8, 4)).toBe("WAVE");
        expect(text(buffer, 36, 4)).toBe("data");
        expect(field(buffer, 20, 2)).toBe(1); // uncompressed PCM
        expect(field(buffer, 22, 2)).toBe(1); // mono
        expect(field(buffer, 24, 4)).toBe(TARGET_RATE);
        expect(field(buffer, 34, 2)).toBe(16); // bits per sample
    });

    it("sizes the header against the samples it carries", () => {
        const buffer = encodeWav(new Float32Array(100), TARGET_RATE);

        expect(buffer.byteLength).toBe(44 + 200);
        expect(field(buffer, 4, 4)).toBe(36 + 200);
        expect(field(buffer, 40, 4)).toBe(200);
    });

    it("scales samples to full range", () => {
        const view = new DataView(encodeWav(new Float32Array([1, -1, 0]), TARGET_RATE));

        expect(view.getInt16(44, true)).toBe(32767);
        expect(view.getInt16(46, true)).toBe(-32767);
        expect(view.getInt16(48, true)).toBe(0);
    });

    it("clamps a sample past full scale instead of wrapping it", () => {
        const view = new DataView(encodeWav(new Float32Array([2, -2]), TARGET_RATE));

        // Wrapping would turn the loudest part of an utterance into a click,
        // which is worse than the clipping it would be hiding.
        expect(view.getInt16(44, true)).toBe(32767);
        expect(view.getInt16(46, true)).toBe(-32767);
    });
});

/** A decoded clip, shaped like the one an AudioContext hands back. */
function decoded(channels: Float32Array[], sampleRate: number): AudioBuffer {
    return {
        numberOfChannels: channels.length,
        sampleRate,
        getChannelData: (i: number) => channels[i] as Float32Array,
    } as unknown as AudioBuffer;
}

describe("decoding what was recorded", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
    });

    function install(buffer: AudioBuffer): { closed: boolean } {
        const state = { closed: false };
        class FakeContext {
            async decodeAudioData(): Promise<AudioBuffer> {
                return buffer;
            }
            close(): void {
                state.closed = true;
            }
        }
        Reflect.set(window, "AudioContext", FakeContext);
        return state;
    }

    it("re-encodes a recording as a mono clip at the target rate", async () => {
        install(decoded([new Float32Array(960), new Float32Array(960)], 48000));

        const wav = await toWav(new Blob([new Uint8Array(8)]));
        const header = new DataView(await wav.arrayBuffer());

        expect(wav.type).toBe("audio/wav");
        expect(header.getUint16(22, true)).toBe(1); // stereo came in, mono goes out
        expect(header.getUint32(24, true)).toBe(TARGET_RATE);
        // 20ms at 48k arrives as 20ms at 16k.
        expect(header.getUint32(40, true)).toBe(320 * 2);
    });

    it("releases the context even though the clip is already encoded", async () => {
        const state = install(decoded([new Float32Array(160)], TARGET_RATE));

        await toWav(new Blob([new Uint8Array(8)]));

        // An AudioContext holds an audio device open until it is closed, and one
        // per utterance is a hardware handle leaked per press of the button.
        expect(state.closed).toBe(true);
    });

    it("says so when the browser cannot decode at all", async () => {
        Reflect.deleteProperty(window, "AudioContext");

        await expect(toWav(new Blob([new Uint8Array(8)]))).rejects.toThrow(/cannot decode/);
    });
});
