/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Microphone audio as a stream of frames, for a recogniser that listens as you
 * speak.
 *
 * Distinct from the recorder beside it, which captures a whole utterance and
 * sends it once. Here nothing is buffered to the end: frames go out while the
 * person is still talking, which is the only way words can come back before they
 * stop.
 *
 * The recogniser wants mono 32-bit float at 16 kHz; a browser gives whatever its
 * hardware runs at, usually 48 kHz. The conversion is the same one the recorder
 * does, reused rather than repeated.
 *
 * Capture goes through a ScriptProcessorNode, which is deprecated in favour of
 * AudioWorklet. At a tenth of a second per frame the main-thread hop it is
 * faulted for costs nothing anyone can hear, while a worklet would need its own
 * module file and message plumbing to reach the same place.
 */

import { resample, toMono, TARGET_RATE } from "./wav";

/** How much audio each frame carries, at the recogniser's rate. */
const FRAME_SAMPLES = 1600; // 100 ms

/** What the browser hands us per callback. Larger means fewer, later frames. */
const CAPTURE_BUFFER = 4096;

type AudioContextCtor = new () => AudioContext;

function audioContext(): AudioContextCtor | undefined {
    const scope = window as { AudioContext?: AudioContextCtor; webkitAudioContext?: AudioContextCtor };
    return scope.AudioContext ?? scope.webkitAudioContext;
}

/** Whether this browser can capture audio frame by frame. */
export function canCaptureLive(): boolean {
    return (
        typeof navigator !== "undefined" &&
        !!navigator.mediaDevices?.getUserMedia &&
        audioContext() !== undefined
    );
}

/** Pack mono float samples as the little-endian float32 the recogniser reads. */
export function framePayload(samples: Float32Array): ArrayBuffer {
    const buffer = new ArrayBuffer(samples.length * 4);
    const view = new DataView(buffer);
    for (let i = 0; i < samples.length; i++) {
        view.setFloat32(i * 4, samples[i] ?? 0, true);
    }
    return buffer;
}

/**
 * Cuts a stream of arbitrary-length blocks into frames of one size.
 *
 * The browser delivers whatever its buffer holds and resampling changes the
 * count again, so neither is a frame boundary. Holding the remainder keeps the
 * audio continuous across callbacks -- dropping it would punch a hole in every
 * one of them.
 */
export class FrameCutter {
    private _pending = new Float32Array(0);

    constructor(private size = FRAME_SAMPLES) {}

    take(block: Float32Array): Float32Array[] {
        const joined = new Float32Array(this._pending.length + block.length);
        joined.set(this._pending);
        joined.set(block, this._pending.length);

        const frames: Float32Array[] = [];
        let offset = 0;
        while (joined.length - offset >= this.size) {
            frames.push(joined.slice(offset, offset + this.size));
            offset += this.size;
        }
        this._pending = joined.slice(offset);
        return frames;
    }

    /** Whatever is left, padded to a whole frame, for the end of a turn. */
    flush(): Float32Array[] {
        if (this._pending.length === 0) {
            return [];
        }
        const last = new Float32Array(this.size);
        last.set(this._pending);
        this._pending = new Float32Array(0);
        return [last];
    }
}

/** A microphone held open, delivering frames until it is stopped. */
export class LiveCapture {
    private _stream: MediaStream | null = null;
    private _context: AudioContext | null = null;
    private _node: ScriptProcessorNode | null = null;
    private _cutter = new FrameCutter();
    private _active = false;

    /** Open the microphone and call `onFrame` as audio arrives. Starting while
     *  already listening does nothing. */
    async start(onFrame: (frame: ArrayBuffer) => void): Promise<void> {
        if (this._active) {
            return;
        }
        const Ctx = audioContext();
        if (!Ctx) {
            throw new Error("this browser cannot capture audio");
        }
        // Claimed before the first await, not after: two quick presses would
        // otherwise both reach getUserMedia, and the second would take over the
        // fields holding the first microphone, leaving it open with nothing
        // left to close it.
        this._active = true;
        try {
            await this._open(Ctx, onFrame);
        } catch (error) {
            // A half-built pipeline still holds the microphone if getUserMedia
            // already resolved.
            this.stop();
            throw error;
        }
    }

    private async _open(Ctx: AudioContextCtor, onFrame: (frame: ArrayBuffer) => void): Promise<void> {
        // Asked for explicitly rather than left to the browser: the recogniser
        // reports that it wants gain control and noise suppression applied
        // before it sees the audio, and raw capture has neither.
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        if (!this._active) {
            // Stopped while the permission prompt was open. Nothing holds this
            // stream now, and the fields it would go in have already been
            // cleared, so it is released here or not at all.
            stream.getTracks().forEach(track => track.stop());
            return;
        }
        this._stream = stream;
        this._context = new Ctx();
        const source = this._context.createMediaStreamSource(this._stream);
        this._node = this._context.createScriptProcessor(CAPTURE_BUFFER, 1, 1);

        const from = this._context.sampleRate;
        this._node.onaudioprocess = event => {
            const block = toMono([event.inputBuffer.getChannelData(0)]);
            for (const frame of this._cutter.take(resample(block, from, TARGET_RATE))) {
                onFrame(framePayload(frame));
            }
        };
        source.connect(this._node);
        this._node.connect(this._context.destination);
    }

    /** Release the microphone, returning any audio not yet sent. Send that tail
     *  before `stopListening`, so the recogniser sees it as part of the turn. */
    stop(): ArrayBuffer[] {
        const tail = this._cutter.flush().map(framePayload);
        if (this._node) {
            this._node.onaudioprocess = null;
            this._node.disconnect();
        }
        // Tracks stopped explicitly: closing the context alone leaves the
        // browser's recording indicator on, which reads as still listening.
        this._stream?.getTracks().forEach(track => track.stop());
        void this._context?.close();
        this._node = null;
        this._stream = null;
        this._context = null;
        this._active = false;
        return tail;
    }
}
