/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, afterEach, vi, beforeEach } from "vitest";

import { LiveMic, attachLiveSocket, liveMic, type LiveSocket } from "../../../ext/stt/LiveMic";

/** A socket that records what the microphone asked it to do. */
function fakeSocket(): LiveSocket & {
    calls: string[];
    frames: ArrayBuffer[];
    say: (text: string, segment?: number) => void;
    settle: (text: string, segment?: number) => void;
    fail: (message: string) => void;
    deafen: () => void;
} {
    let heard: (text: string, segment: number, final: boolean) => void = () => {};
    let failed: (message: string) => void = () => {};
    let open = true;
    const calls: string[] = [];
    const frames: ArrayBuffer[] = [];
    return {
        calls,
        frames,
        startListening: () => {
            calls.push("start");
            return true;
        },
        stopListening: () => {
            calls.push("stop");
            return true;
        },
        sendAudio: (frame: ArrayBuffer) => {
            if (!open) {
                return false;
            }
            calls.push("frame");
            frames.push(frame);
            return true;
        },
        onTranscript: fn => {
            heard = fn;
        },
        onTranscriptError: fn => {
            failed = fn;
        },
        say: (text, segment = 0) => heard(text, segment, false),
        settle: (text, segment = 0) => heard(text, segment, true),
        fail: message => failed(message),
        deafen: () => {
            open = false;
        },
    };
}

/** The microphone and audio pipeline, so a turn can run without either. */
function installAudio(): { emit: (block: Float32Array) => void } {
    let deliver: ((e: any) => void) | null = null;
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
    class FakeContext {
        sampleRate = 16000;
        destination = {};
        createMediaStreamSource() {
            return { connect: vi.fn() };
        }
        audioWorklet = { addModule: vi.fn(async () => {}) };
        close() {}
    }
    Reflect.set(window, "AudioContext", FakeContext);
    Reflect.set(navigator, "mediaDevices", {
        getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }] })),
    });
    return {
        emit: block => deliver?.({ data: block }),
    };
}

describe("a turn at the microphone", () => {
    let audio: ReturnType<typeof installAudio>;

    beforeEach(() => {
        audio = installAudio();
    });

    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("shows words on top of what the composer already held", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const seen: string[] = [];
        await mic.start("note:", { onText: t => seen.push(t), onEnd: () => {} });

        socket.say("HELLO");

        expect(seen).toEqual(["note: Hello"]);
        mic.stop();
    });

    it("replaces a reading rather than repeating it", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const seen: string[] = [];
        await mic.start("", { onText: t => seen.push(t), onEnd: () => {} });

        socket.say("hello th");
        socket.say("hello there");

        expect(seen.at(-1)).toBe("hello there");
        mic.stop();
    });

    it("sends the last audio before saying the turn is over", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        await mic.start("", { onText: () => {}, onEnd: () => {} });
        // Less than a frame, so it only leaves on the way out.
        audio.emit(new Float32Array(800));

        mic.stop();

        // The server closes the turn on "stop", so a tail behind it is lost.
        expect(socket.calls).toEqual(["start", "frame", "stop"]);
    });

    it("ends the turn when the connection drops", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });

        socket.deafen();
        audio.emit(new Float32Array(1600));

        // Frames after a reconnect reach a server that has forgotten the turn,
        // so the button must not stay lit with words silently going nowhere.
        expect(ends).toEqual(["connection lost"]);
        expect(mic.listening).toBe(false);
    });

    it("ends the turn when recognition fails", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });

        socket.fail("recogniser gone");

        expect(ends).toEqual(["recogniser gone"]);
        expect(mic.listening).toBe(false);
    });

    it("keeps the reading of the last words spoken", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const seen: string[] = [];
        await mic.start("", { onText: t => seen.push(t), onEnd: () => {} });
        socket.say("hello th");

        mic.stop();
        // The tail was sent on stop; its reading comes back after it, which is
        // the whole point of sending it.
        socket.settle("hello there");

        expect(seen.at(-1)).toBe("hello there");
    });

    it("is no longer recording once the person ends it", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        await mic.start("", { onText: () => {}, onEnd: () => {} });

        mic.stop();

        // The microphone is shut even though the last reading is still coming.
        expect(mic.listening).toBe(false);
    });

    it("ends the turn once the last reading settles", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
        mic.stop();

        expect(ends).toEqual([]);
        socket.settle("hello there");

        expect(ends).toEqual([undefined]);
    });

    it("gives up waiting if the reading never comes", async () => {
        vi.useFakeTimers();
        try {
            const socket = fakeSocket();
            const mic = new LiveMic(socket);
            const ends: (string | undefined)[] = [];
            await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
            mic.stop();

            vi.advanceTimersByTime(5000);

            // A recogniser that stops answering must not leave the turn open.
            expect(ends).toEqual([undefined]);
        } finally {
            vi.useRealTimers();
        }
    });

    it("reports no reason when the person ends it", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });

        mic.stop();
        socket.settle("hello there");

        // A reason means something went wrong; ending it deliberately is not.
        expect(ends).toEqual([undefined]);
    });

    it("releases a microphone granted after the turn was ended", async () => {
        let allow: ((s: unknown) => void) | undefined;
        const tracks = [{ stop: vi.fn() }];
        Reflect.set(navigator, "mediaDevices", {
            getUserMedia: vi.fn(
                () =>
                    new Promise(resolve => {
                        allow = resolve;
                    }),
            ),
        });
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const starting = mic.start("", { onText: () => {}, onEnd: () => {} });

        // Ended while the permission prompt was still open.
        mic.stop();
        allow!({ getTracks: () => tracks });
        await starting;

        // Nothing references this stream any more, so it is released here or the
        // browser goes on showing the recording indicator.
        expect(tracks[0]!.stop).toHaveBeenCalled();
        expect(mic.listening).toBe(false);
    });

    it("does not wait for a reading of audio it never recorded", async () => {
        vi.useFakeTimers();
        try {
            let allow: ((s: unknown) => void) | undefined;
            Reflect.set(navigator, "mediaDevices", {
                getUserMedia: vi.fn(
                    () =>
                        new Promise(resolve => {
                            allow = resolve;
                        }),
                ),
            });
            const socket = fakeSocket();
            const mic = new LiveMic(socket);
            const ends: (string | undefined)[] = [];
            const starting = mic.start("", { onText: () => {}, onEnd: r => ends.push(r) });

            mic.stop();
            allow!({ getTracks: () => [{ stop: vi.fn() }] });
            await starting;

            // Ended before a single frame, so the turn is over now rather than
            // when the drain runs out.
            expect(ends).toEqual([undefined]);
        } finally {
            vi.useRealTimers();
        }
    });

    it("says so when the last audio cannot be sent", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
        audio.emit(new Float32Array(800));

        socket.deafen();
        mic.stop();

        // Nothing can come back, so waiting out the drain would end the turn as
        // though the words had been heard.
        expect(ends).toEqual(["connection lost"]);
    });

    it("lets the person speak again before the last reading arrives", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
        mic.stop();

        await mic.start("", { onText: () => {}, onEnd: () => {} });

        // The drained turn is closed out and a new one is open, rather than the
        // button doing nothing at all.
        expect(ends).toEqual([undefined]);
        expect(mic.listening).toBe(true);
        mic.stop();
    });

    it("says so when the turn cannot even be closed", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
        socket.stopListening = () => false;

        mic.stop();

        // The server never hears that the turn ended, so no settled reading is
        // coming and waiting out the drain would end it as though one had.
        expect(ends).toEqual(["connection lost"]);
    });

    it("treats a socket that throws as one that refused", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const ends: (string | undefined)[] = [];
        await mic.start("", { onText: () => {}, onEnd: reason => ends.push(reason) });
        socket.sendAudio = () => {
            throw new Error("socket is closing");
        };

        audio.emit(new Float32Array(1600));

        // This runs inside the audio callback, where nothing is waiting to catch
        // it: an uncaught throw there leaves the microphone open for good.
        expect(ends).toEqual(["connection lost"]);
        expect(mic.listening).toBe(false);
    });

    it("refuses to start without a connection", async () => {
        const socket = fakeSocket();
        socket.startListening = () => false;
        const mic = new LiveMic(socket);

        await expect(mic.start("", { onText: () => {}, onEnd: () => {} })).rejects.toThrow(/not connected/);
        expect(mic.listening).toBe(false);
    });

    it("tells the server to stop when the microphone will not open", async () => {
        Reflect.set(navigator, "mediaDevices", {
            getUserMedia: vi.fn(async () => {
                throw new Error("denied");
            }),
        });
        const socket = fakeSocket();
        const mic = new LiveMic(socket);

        await expect(mic.start("", { onText: () => {}, onEnd: () => {} })).rejects.toThrow();

        // Otherwise the server holds a session open for audio that never comes.
        expect(socket.calls).toEqual(["start", "stop"]);
        expect(mic.listening).toBe(false);
    });

    it("a second start while listening changes nothing", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        await mic.start("", { onText: () => {}, onEnd: () => {} });

        await mic.start("", { onText: () => {}, onEnd: () => {} });

        expect(socket.calls.filter(c => c === "start").length).toBe(1);
        mic.stop();
    });

    it("stopping when idle does nothing", () => {
        const socket = fakeSocket();
        new LiveMic(socket).stop();
        expect(socket.calls).toEqual([]);
    });

    it("does not carry one turn's words into the next", async () => {
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const seen: string[] = [];
        await mic.start("", { onText: t => seen.push(t), onEnd: () => {} });
        socket.say("first");
        mic.stop();

        await mic.start("", { onText: t => seen.push(t), onEnd: () => {} });
        socket.say("second");

        expect(seen.at(-1)).toBe("second");
        mic.stop();
    });
});

describe("the page's microphone", () => {
    it("is absent until a socket is attached", () => {
        expect(liveMic()).toBeNull();
    });

    it("is available once attached", () => {
        attachLiveSocket(fakeSocket());
        expect(liveMic()).toBeInstanceOf(LiveMic);
    });
});

describe("starting again while the last permission prompt is still open", () => {
    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(window, "AudioWorkletNode");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("does not let the abandoned turn tear down the one that replaced it", async () => {
        // Press, stop, press again on a slow prompt. The first turn's start is
        // still waiting; when it finally continues, the state it finds belongs to
        // the second turn. Ending "the current turn" there ends the wrong one:
        // the microphone the person is speaking into stops, and the session it
        // opened on the server is never closed.
        installAudio();
        const media = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>;
        let letFirstThrough: (() => void) | null = null;
        media.mockImplementationOnce(
            () =>
                new Promise(resolve => {
                    letFirstThrough = () => resolve({ getTracks: () => [{ stop: vi.fn() }] });
                }),
        );
        const socket = fakeSocket();
        const mic = new LiveMic(socket);
        const first = { onText: vi.fn(), onEnd: vi.fn() };
        const second = { onText: vi.fn(), onEnd: vi.fn() };

        const abandoned = mic.start("", first);
        mic.stop();
        await mic.start("", second);
        letFirstThrough!();
        await abandoned;

        // The turn someone is actually speaking into is still running.
        expect(second.onEnd).not.toHaveBeenCalled();
        expect(mic.listening).toBe(true);
    });
});
