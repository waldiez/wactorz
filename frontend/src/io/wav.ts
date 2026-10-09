/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Turn a browser recording into the WAV the server's `/api/stt` accepts.
 *
 * MediaRecorder produces WebM/Opus in Chrome and MP4 in Safari. Decoding here,
 * with the browser's own codecs, means the server needs no audio decoder and
 * every recognizer behind it receives the same 16 kHz mono 16-bit PCM that
 * Reachy's microphone provides.
 */

/** Sample rate the server's recognizers are given. */
export const STT_SAMPLE_RATE = 16_000;

/** Encode mono float samples (-1..1) as a 16-bit PCM WAV file. */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeText = (offset: number, text: string): void => {
        for (let i = 0; i < text.length; i++) {
            view.setUint8(offset + i, text.charCodeAt(i));
        }
    };
    writeText(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeText(8, "WAVE");
    writeText(12, "fmt ");
    view.setUint32(16, 16, true); // fmt chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true); // byte rate
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    writeText(36, "data");
    view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
        const clamped = Math.max(-1, Math.min(1, samples[i] ?? 0));
        view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    }
    return buffer;
}

/** Average an audio buffer's channels into one. */
export function downmix(audio: AudioBuffer): Float32Array {
    const mono = new Float32Array(audio.length);
    for (let channel = 0; channel < audio.numberOfChannels; channel++) {
        const data = audio.getChannelData(channel);
        for (let i = 0; i < data.length; i++) {
            mono[i] = (mono[i] ?? 0) + (data[i] ?? 0) / audio.numberOfChannels;
        }
    }
    return mono;
}

/**
 * Decode a recording and re-encode it as 16 kHz mono WAV.
 *
 * Decoding through an OfflineAudioContext at 16 kHz also resamples, since
 * `decodeAudioData` returns audio at its context's rate.
 */
export async function toWav16k(recording: Blob): Promise<Blob> {
    const Offline = (globalThis as { OfflineAudioContext?: typeof OfflineAudioContext }).OfflineAudioContext;
    if (!Offline) {
        throw new Error("This browser cannot prepare audio for the server.");
    }
    const context = new Offline(1, 1, STT_SAMPLE_RATE);
    const decoded = await context.decodeAudioData(await recording.arrayBuffer());
    return new Blob([new Uint8Array(encodeWav(downmix(decoded), decoded.sampleRate))], { type: "audio/wav" });
}
