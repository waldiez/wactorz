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
 * Capture runs on the audio thread, in a worklet. The main thread is busy
 * drawing the reply that is streaming in while someone is still talking, and a
 * capture that shares it loses whatever arrives during a long frame -- silently,
 * as a gap in the words rather than an error. Here a busy main thread delays the
 * frames instead of dropping them.
 *
 * The worklet is loaded from a blob rather than a file it would have to be
 * bundled as, which keeps it beside the code that uses it.
 */

import { resample, TARGET_RATE } from "./wav";

/** How much audio each frame carries, at the recogniser's rate. */
const FRAME_SAMPLES = 1600; // 100 ms

/** How much the worklet gathers before handing it over, in samples at the
 *  hardware's rate. A multiple of the 128-sample quantum the audio thread runs
 *  in; larger means fewer messages and later frames. */
const CAPTURE_BLOCK = 2048;

/** The worklet itself. It only gathers and forwards: converting rates and
 *  cutting frames is the same work either way, and is left where it is tested.
 *
 *  Exported because it runs in a scope no test has, so a test builds that scope
 *  around it -- a mistake in here reaches a person as silence, not an error. */
export const PROCESSOR = `
class CaptureProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this._block = new Float32Array(options.processorOptions.block);
        this._filled = 0;
    }
    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel) {
            return true;
        }
        for (let i = 0; i < channel.length; i++) {
            this._block[this._filled++] = channel[i];
            if (this._filled === this._block.length) {
                this.port.postMessage(this._block.slice());
                this._filled = 0;
            }
        }
        return true;
    }
}
registerProcessor("wactorz-capture", CaptureProcessor);
`;

type AudioContextCtor = new () => AudioContext;

/** The worklet as something `addModule` can fetch. */
function workletUrl(): string {
    return URL.createObjectURL(new Blob([PROCESSOR], { type: "text/javascript" }));
}

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
    private _node: AudioWorkletNode | null = null;
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
        if (!this._context.audioWorklet) {
            throw new Error("this browser cannot capture audio");
        }
        const url = workletUrl();
        try {
            await this._context.audioWorklet.addModule(url);
        } finally {
            // The worklet is compiled by now, so the blob has served its purpose
            // and would otherwise be held for the life of the page.
            URL.revokeObjectURL(url);
        }

        const source = this._context.createMediaStreamSource(this._stream);
        this._node = new AudioWorkletNode(this._context, "wactorz-capture", {
            numberOfInputs: 1,
            numberOfOutputs: 0,
            // Mixed down here rather than in the worklet: a stereo microphone
            // would otherwise be heard through its left side alone, which is
            // quieter and misses whatever the other side picked up.
            channelCount: 1,
            channelCountMode: "explicit",
            processorOptions: { block: CAPTURE_BLOCK },
        });

        const from = this._context.sampleRate;
        this._node.port.onmessage = event => {
            const block = event.data as Float32Array;
            for (const frame of this._cutter.take(resample(block, from, TARGET_RATE))) {
                onFrame(framePayload(frame));
            }
        };
        // No output: the microphone is being read, not played, and connecting to
        // the destination would put the room through the speakers.
        source.connect(this._node);
    }

    /** Release the microphone, returning any audio not yet sent. Send that tail
     *  before `stopListening`, so the recogniser sees it as part of the turn. */
    stop(): ArrayBuffer[] {
        const tail = this._cutter.flush().map(framePayload);
        if (this._node) {
            this._node.port.onmessage = null;
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
