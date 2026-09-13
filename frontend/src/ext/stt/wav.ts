/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Turn recorded audio into the WAV the recogniser accepts.
 *
 * MediaRecorder produces WebM/Opus and nothing else worth relying on, while a
 * Wyoming recogniser wants plain PCM. Converting here rather than server-side
 * keeps a codec out of the server, where it would have meant a system package
 * standing between the feature and working at all. The browser already carries
 * one for playback.
 *
 * The arithmetic is separated from the decoding so it can be tested: decoding
 * needs an AudioContext, which a DOM built for tests does not have.
 */

/** What Wyoming ASR services expect, Whisper included. */
export const TARGET_RATE = 16000;

/** Average channels down to one, since recognition has no use for a stereo image. */
export function toMono(channels: Float32Array[]): Float32Array {
    if (channels.length === 0) {
        return new Float32Array(0);
    }
    const [first] = channels;
    if (channels.length === 1 || !first) {
        return first ?? new Float32Array(0);
    }
    const out = new Float32Array(first.length);
    for (let i = 0; i < out.length; i++) {
        let sum = 0;
        for (const channel of channels) {
            sum += channel[i] ?? 0;
        }
        out[i] = sum / channels.length;
    }
    return out;
}

/**
 * Resample by linear interpolation.
 *
 * Enough for speech at these rates: the browser has already low-passed the
 * signal at its own capture rate, and what recognition needs from a resampler
 * is the right number of samples at the right pitch rather than an anti-aliased
 * one. A better filter would cost more code than the accuracy is worth here.
 */
export function resample(samples: Float32Array, from: number, to: number): Float32Array {
    if (from === to || samples.length === 0) {
        return samples;
    }
    const ratio = from / to;
    const out = new Float32Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < out.length; i++) {
        const position = i * ratio;
        const left = Math.floor(position);
        const right = Math.min(left + 1, samples.length - 1);
        const drift = position - left;
        out[i] = (samples[left] ?? 0) * (1 - drift) + (samples[right] ?? 0) * drift;
    }
    return out;
}

/** Encode mono samples as a 16-bit PCM WAV, header and all. */
export function encodeWav(samples: Float32Array, rate: number): ArrayBuffer {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const text = (offset: number, value: string) => {
        for (let i = 0; i < value.length; i++) {
            view.setUint8(offset + i, value.charCodeAt(i));
        }
    };

    text(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true); // PCM header length
    view.setUint16(20, 1, true); // uncompressed
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); // bytes per second
    view.setUint16(32, 2, true); // bytes per frame
    view.setUint16(34, 16, true); // bits per sample
    text(36, "data");
    view.setUint32(40, samples.length * 2, true);

    for (let i = 0; i < samples.length; i++) {
        // Clamped before scaling: a sample past full scale would otherwise wrap
        // to the opposite extreme and read as a click rather than as loudness.
        const sample = Math.max(-1, Math.min(1, samples[i] ?? 0));
        view.setInt16(44 + i * 2, Math.round(sample * 32767), true);
    }
    return buffer;
}

/** Decode whatever was recorded and re-encode it as a mono 16 kHz WAV clip. */
export async function toWav(blob: Blob): Promise<Blob> {
    const Ctx =
        window.AudioContext ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) {
        throw new Error("this browser cannot decode recorded audio");
    }
    const context = new Ctx();
    try {
        const decoded = await context.decodeAudioData(await blob.arrayBuffer());
        const channels = Array.from({ length: decoded.numberOfChannels }, (_, i) =>
            decoded.getChannelData(i),
        );
        const mono = resample(toMono(channels), decoded.sampleRate, TARGET_RATE);
        return new Blob([encodeWav(mono, TARGET_RATE)], { type: "audio/wav" });
    } finally {
        void context.close();
    }
}
