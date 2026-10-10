/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Cutting captured audio into the frames a streaming recogniser expects.
 *
 * The browser delivers blocks of its own choosing and resampling changes the
 * count again, so the arithmetic between the microphone and the wire is where
 * audio goes missing. Opening the microphone itself needs an AudioContext the
 * DOM these tests run in does not have; the parts that can be checked are.
 */
import { describe, it, expect, afterEach, vi } from "vitest";

import { FrameCutter, framePayload, canCaptureLive, LiveCapture } from "../../../ext/stt/liveCapture";
// The file the browser is served, read as text so the half that runs on the
// audio thread can be run here too.
import PROCESSOR from "../../../../public/capture-worklet.js?raw";

describe("cutting blocks into frames", () => {
    it("emits nothing until a whole frame is ready", () => {
        const cutter = new FrameCutter(4);

        expect(cutter.take(new Float32Array([1, 2]))).toEqual([]);
    });

    it("emits a frame once enough has arrived", () => {
        const cutter = new FrameCutter(4);
        cutter.take(new Float32Array([1, 2]));

        const frames = cutter.take(new Float32Array([3, 4, 5]));

        expect(frames.length).toBe(1);
        expect(Array.from(frames[0]!)).toEqual([1, 2, 3, 4]);
    });

    it("carries the remainder into the next block", () => {
        const cutter = new FrameCutter(4);
        cutter.take(new Float32Array([1, 2, 3, 4, 5]));

        const frames = cutter.take(new Float32Array([6, 7, 8]));

        // The 5 must not be lost: dropping each remainder would punch a hole in
        // the audio at every callback boundary.
        expect(Array.from(frames[0]!)).toEqual([5, 6, 7, 8]);
    });

    it("emits several frames from one large block", () => {
        const cutter = new FrameCutter(2);

        expect(cutter.take(new Float32Array([1, 2, 3, 4, 5, 6])).length).toBe(3);
    });

    it("pads the last partial frame at the end of a turn", () => {
        const cutter = new FrameCutter(4);
        cutter.take(new Float32Array([1, 2]));

        const tail = cutter.flush();

        expect(tail.length).toBe(1);
        expect(Array.from(tail[0]!)).toEqual([1, 2, 0, 0]);
    });

    it("has nothing to flush when the audio divided evenly", () => {
        const cutter = new FrameCutter(2);
        cutter.take(new Float32Array([1, 2, 3, 4]));

        expect(cutter.flush()).toEqual([]);
    });
});

describe("packing a frame for the wire", () => {
    it("writes little-endian 32-bit floats", () => {
        const view = new DataView(framePayload(new Float32Array([0.5, -0.5])));

        expect(view.byteLength).toBe(8);
        expect(view.getFloat32(0, true)).toBeCloseTo(0.5);
        expect(view.getFloat32(4, true)).toBeCloseTo(-0.5);
    });

    it("produces four bytes per sample", () => {
        expect(framePayload(new Float32Array(1600)).byteLength).toBe(6400);
    });
});

describe("whether this browser can capture live", () => {
    it("says no when there is no audio context", () => {
        // happy-dom has neither, which is also what an old browser looks like.
        expect(canCaptureLive()).toBe(false);
    });
});

/** A microphone and audio pipeline, so the wiring can be driven without either. */
function installAudio(sampleRate = 16000): {
    modules: () => string[];
    breakWorklet: (failure: Error) => void;
    removeWorkletSupport: () => void;
    destinationNode: () => unknown;
    connectedTo: () => unknown[];
    emit: (block: Float32Array) => void;
    stopped: () => number;
    closed: () => boolean;
    breakSetup: (broken: boolean) => void;
} {
    const tracks = [{ stop: vi.fn() }, { stop: vi.fn() }];
    let deliver: ((e: any) => void) | null = null;
    let closed = false;
    let broken = false;

    const node = {
        connect: vi.fn(),
        disconnect: vi.fn(),
        port: {
            set onmessage(fn: ((e: any) => void) | null) {
                deliver = fn;
            },
            get onmessage() {
                return deliver;
            },
        },
    };
    Reflect.set(
        window,
        "AudioWorkletNode",
        class {
            constructor() {
                return node;
            }
        },
    );

    const added: string[] = [];
    let workletFailure: Error | null = null;
    let worklets = true;
    const speakers = {};
    const source = { connect: vi.fn() };

    class FakeContext {
        sampleRate = sampleRate;
        destination = speakers;
        audioWorklet: { addModule: (url: string) => Promise<void> } | undefined = {
            addModule: vi.fn(async (url: string) => {
                if (workletFailure) {
                    throw workletFailure;
                }
                added.push(url);
            }),
        };
        createMediaStreamSource() {
            if (broken) {
                throw new Error("no such device");
            }
            return source;
        }

        close() {
            closed = true;
        }

        constructor() {
            if (!worklets) {
                this.audioWorklet = undefined;
            }
        }
    }

    Reflect.set(window, "AudioContext", FakeContext);
    Reflect.set(navigator, "mediaDevices", {
        getUserMedia: vi.fn(async () => ({ getTracks: () => tracks })),
    });

    return {
        modules: () => added,
        breakWorklet: (failure: Error) => {
            workletFailure = failure;
        },
        removeWorkletSupport: () => {
            worklets = false;
        },
        destinationNode: () => speakers,
        connectedTo: () => source.connect.mock.calls.map(call => call[0]),
        emit: block => deliver?.({ data: block }),
        stopped: () => tracks.filter(t => t.stop.mock.calls.length > 0).length,
        closed: () => closed,
        breakSetup: (value: boolean) => {
            broken = value;
        },
    };
}

describe("holding the microphone open", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("delivers frames while audio arrives", async () => {
        const audio = installAudio();
        const capture = new LiveCapture();
        const frames: ArrayBuffer[] = [];
        await capture.start(f => frames.push(f));

        audio.emit(new Float32Array(1600));

        // 100 ms at the recogniser's rate, four bytes a sample.
        expect(frames.length).toBe(1);
        expect(frames[0]!.byteLength).toBe(6400);
        capture.stop();
    });

    it("resamples what the hardware gives to what the recogniser wants", async () => {
        const audio = installAudio(48000);
        const capture = new LiveCapture();
        const frames: ArrayBuffer[] = [];
        await capture.start(f => frames.push(f));

        // 4800 samples at 48 kHz is 100 ms, which is exactly one frame at 16 kHz.
        audio.emit(new Float32Array(4800));

        expect(frames.length).toBe(1);
        capture.stop();
    });

    it("captures on the audio thread, not this one", async () => {
        const audio = installAudio();
        const capture = new LiveCapture();

        await capture.start(() => {});

        // A capture sharing the main thread loses whatever arrives while the
        // page is busy drawing, which is exactly when someone is talking.
        expect(audio.modules().length).toBe(1);
        capture.stop();
    });

    it("does not put the microphone through the speakers", async () => {
        const audio = installAudio();
        const capture = new LiveCapture();

        await capture.start(() => {});

        // The node is read, never played: connecting it onward would put the
        // room through the speakers and feed it back in.
        expect(audio.connectedTo().length).toBe(1);
        expect(audio.connectedTo()[0]).not.toBe(audio.destinationNode());
        capture.stop();
    });

    it("releases the microphone when it stops", async () => {
        const audio = installAudio();
        const capture = new LiveCapture();
        await capture.start(() => {});

        capture.stop();

        // Every track, not just the context: closing the context alone leaves
        // the browser's recording indicator on.
        expect(audio.stopped()).toBe(2);
        expect(audio.closed()).toBe(true);
    });

    it("returns the audio that had not filled a frame yet", async () => {
        const audio = installAudio();
        const capture = new LiveCapture();
        const frames: ArrayBuffer[] = [];
        await capture.start(f => frames.push(f));
        audio.emit(new Float32Array(800));

        const tail = capture.stop();

        expect(frames.length).toBe(0);
        expect(tail.length).toBe(1);
    });

    it("says so when the browser has no worklet to capture in", async () => {
        const audio = installAudio();
        audio.removeWorkletSupport();

        // A browser old enough to lack this reaches the button at all only
        // because everything else it needs is much older.
        await expect(new LiveCapture().start(() => {})).rejects.toThrow(/cannot capture/);
    });

    it("releases the microphone when the worklet will not load", async () => {
        const audio = installAudio();
        audio.breakWorklet(new Error("blocked by the page's policy"));
        const capture = new LiveCapture();

        await expect(capture.start(() => {})).rejects.toThrow(/policy/);

        expect(audio.stopped()).toBe(2);
    });

    it("says so when the browser cannot capture", async () => {
        Reflect.deleteProperty(window, "AudioContext");

        await expect(new LiveCapture().start(() => {})).rejects.toThrow(/cannot capture/);
    });

    it("stopping before starting is harmless", () => {
        expect(new LiveCapture().stop()).toEqual([]);
    });
});

describe("deciding whether the browser can capture", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(window, "webkitAudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("accepts the prefixed context older browsers expose", () => {
        Reflect.set(window, "webkitAudioContext", class {});
        Reflect.set(navigator, "mediaDevices", { getUserMedia: () => {} });

        expect(canCaptureLive()).toBe(true);
    });

    it("declines without a microphone API", () => {
        Reflect.set(window, "AudioContext", class {});
        Reflect.set(navigator, "mediaDevices", {});

        expect(canCaptureLive()).toBe(false);
    });

    it("declines without any audio context", () => {
        Reflect.set(navigator, "mediaDevices", { getUserMedia: () => {} });

        expect(canCaptureLive()).toBe(false);
    });
});

describe("holding the microphone only once", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("ignores a second start while already listening", async () => {
        installAudio();
        const opened = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>;
        const capture = new LiveCapture();

        await capture.start(() => {});
        await capture.start(() => {});

        expect(opened.mock.calls.length).toBe(1);
        capture.stop();
    });

    it("ignores a second start before the first microphone opens", async () => {
        installAudio();
        const opened = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>;
        const capture = new LiveCapture();

        // Both presses land before either await settles, which is what two quick
        // clicks on the button actually do.
        await Promise.all([capture.start(() => {}), capture.start(() => {})]);

        expect(opened.mock.calls.length).toBe(1);
        capture.stop();
    });

    it("releases the microphone when the pipeline fails to build", async () => {
        const audio = installAudio();
        audio.breakSetup(true);
        const capture = new LiveCapture();

        await expect(capture.start(() => {})).rejects.toThrow("no such device");

        expect(audio.stopped()).toBe(2);
    });

    it("can listen again after a failed start", async () => {
        const audio = installAudio();
        audio.breakSetup(true);
        const capture = new LiveCapture();
        await expect(capture.start(() => {})).rejects.toThrow();
        audio.breakSetup(false);

        await expect(capture.start(() => {})).resolves.toBeUndefined();
        capture.stop();
    });
});

/** Run the audio-thread half in a scope shaped like the one the browser gives it. */
function loadProcessor(): new (options: { processorOptions: { block: number } }) => {
    port: { postMessage: ReturnType<typeof vi.fn> };
    process: (inputs: Float32Array[][]) => boolean;
} {
    const registered: Record<string, unknown> = {};

    class FakeBase {
        port = { postMessage: vi.fn() };
    }

    const load = new Function("AudioWorkletProcessor", "registerProcessor", PROCESSOR);
    load(FakeBase, (name: string, ctor: unknown) => {
        registered[name] = ctor;
    });
    return registered["wactorz-capture"] as never;
}

describe("the half that runs on the audio thread", () => {
    it("is valid and registers itself under the name the node asks for", () => {
        // It never runs in this process, so nothing else would notice a mistake
        // in it until someone pressed the button.
        expect(loadProcessor()).toBeTypeOf("function");
    });

    it("hands over a block once it is full, and not before", () => {
        const Processor = loadProcessor();
        const worklet = new Processor({ processorOptions: { block: 4 } });

        worklet.process([[new Float32Array([1, 2, 3])]]);
        expect(worklet.port.postMessage).not.toHaveBeenCalled();

        worklet.process([[new Float32Array([4, 5])]]);

        expect(worklet.port.postMessage).toHaveBeenCalledTimes(1);
        expect([...worklet.port.postMessage.mock.calls[0]![0]]).toEqual([1, 2, 3, 4]);
    });

    it("keeps running when the microphone delivers nothing", () => {
        const Processor = loadProcessor();
        const worklet = new Processor({ processorOptions: { block: 4 } });

        // Returning false here would shut the worklet down for the rest of the
        // turn; an input can be empty between quanta.
        expect(worklet.process([[]])).toBe(true);
        expect(worklet.port.postMessage).not.toHaveBeenCalled();
    });

    it("carries the remainder into the next block", () => {
        const Processor = loadProcessor();
        const worklet = new Processor({ processorOptions: { block: 2 } });

        worklet.process([[new Float32Array([1, 2, 3, 4, 5])]]);

        const posted = worklet.port.postMessage.mock.calls;
        expect(posted.length).toBe(2);
        expect([...posted[1]![0]]).toEqual([3, 4]);
    });
});

describe("stopping and starting again while the permission prompt is open", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("releases the microphone when stopped and not started again", async () => {
        // No restart to bump anything: the stop itself has to make the attempt
        // still inside the prompt find that it is no longer the current one.
        installAudio();
        const media = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>;
        let letItThrough: (() => void) | null = null;
        const tracks = [{ stop: vi.fn() }];
        media.mockImplementationOnce(
            () =>
                new Promise(resolve => {
                    letItThrough = () => resolve({ getTracks: () => tracks });
                }),
        );

        const capture = new LiveCapture();
        const abandoned = capture.start(() => {});
        capture.stop();
        letItThrough!();
        await abandoned;

        expect(tracks[0]!.stop).toHaveBeenCalled();
    });

    it("releases the microphone the abandoned attempt opened", async () => {
        // Three presses on a slow prompt: start, stop, start. The first call is
        // still inside getUserMedia, and by the time it returns the restart has
        // set the flag it checks -- so without a token of its own it walks on and
        // its stream, context and node become unreachable. The browser goes on
        // recording, with the interface showing the microphone off.
        const audio = installAudio();
        const media = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>;
        let letFirstThrough: (() => void) | null = null;
        const tracks = [{ stop: vi.fn() }];
        media.mockImplementationOnce(
            () =>
                new Promise(resolve => {
                    letFirstThrough = () => resolve({ getTracks: () => tracks });
                }),
        );

        const capture = new LiveCapture();
        const abandoned = capture.start(() => {});
        capture.stop();
        await capture.start(() => {});
        letFirstThrough!();
        await abandoned;

        expect(tracks[0]!.stop).toHaveBeenCalled();
        // And exactly one pipeline is left holding the device.
        expect(audio.modules().length).toBe(1);
        capture.stop();
    });
});
