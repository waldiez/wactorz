/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// The live branch of the mic button, which needs a recogniser that streams and a
// browser that can deliver frames. Both are forced on here so the branch can be
// driven; what decides between them is covered in ext/stt/mode.test.ts.
vi.mock("../ext/stt", async importOriginal => ({
    ...(await importOriginal<typeof import("../ext/stt")>()),
    micOffered: () => true,
    liveOffered: () => true,
}));
vi.mock("../ui/dashboard/uploads", async importOriginal => ({
    ...(await importOriginal<typeof import("../ui/dashboard/uploads")>()),
    uploadsEnabled: () => false,
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import { attachLiveSocket, type LiveSocket } from "../ext/stt";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../ext/stt";
import { toast } from "../ui/ToastManager";

function fakeSocket(): LiveSocket & {
    say: (t: string) => void;
    settle: (t: string) => void;
    fail: (m: string) => void;
} {
    let heard: (text: string, segment: number, final: boolean) => void = () => {};
    let failed: (message: string) => void = () => {};
    return {
        startListening: () => true,
        stopListening: () => true,
        sendAudio: () => true,
        onTranscript: fn => {
            heard = fn;
        },
        onTranscriptError: fn => {
            failed = fn;
        },
        say: text => heard(text, 0, false),
        settle: text => heard(text, 0, true),
        fail: message => failed(message),
    };
}

function installAudio(): void {
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
    Reflect.set(
        window,
        "AudioWorkletNode",
        class {
            constructor() {
                return { connect: vi.fn(), disconnect: vi.fn(), port: { onmessage: null } };
            }
        },
    );
    Reflect.set(navigator, "mediaDevices", {
        getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }] })),
    });
}

function mount(): HTMLElement {
    const deps: IobarDeps = {
        chatInput: { onChange: vi.fn(), onKeydown: vi.fn(), closePanel: vi.fn() } as unknown as ChatInput,
        stt: { recording: false } as unknown as SpeechToText,
        target: () => "main",
        setTarget: vi.fn(),
        populateSelect: vi.fn(),
        send: vi.fn(),
        stop: vi.fn(),
    };
    const bar = buildIobar(deps);
    document.body.appendChild(bar);
    return bar;
}

describe("the mic button with a recogniser that streams", () => {
    let socket: ReturnType<typeof fakeSocket>;

    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        installAudio();
        socket = fakeSocket();
        attachLiveSocket(socket);
    });

    afterEach(() => {
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
    });

    it("shows words in the composer while they are spoken", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.say("hello there");

        expect(input.value).toBe("hello there");
    });

    it("adds to a half-written message instead of replacing it", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        input.value = "note:";

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.say("buy milk");

        expect(input.value).toBe("note: buy milk");
    });

    it("stops listening on the second click", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        btn.click();

        // Unlit at once: the microphone is shut, even though the reading of the
        // last words has not arrived yet.
        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("still shows the words spoken just before the click", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.say("hello th");
        btn.click();
        socket.settle("hello there");

        expect(input.value).toBe("hello there");
    });

    it("clears the lit button and says why when recognition dies", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.fail("recogniser gone");

        expect(btn.classList.contains("recording")).toBe(false);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ message: "recogniser gone" }));
    });

    it("names the browser, not the person, when capture is impossible", async () => {
        // A browser that cannot capture was never asked for permission, so
        // naming one is a dead end for whoever reads the message.
        Reflect.set(navigator, "mediaDevices", {
            getUserMedia: vi.fn(async () => {
                throw new Error("this browser cannot capture audio");
            }),
        });
        const btn = mount().querySelector<HTMLButtonElement>(".af-mic-btn")!;

        btn.click();

        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ message: "this browser cannot capture audio" }),
            ),
        );
    });

    it("toasts when the microphone is refused", async () => {
        Reflect.set(navigator, "mediaDevices", {
            getUserMedia: vi.fn(async () => {
                throw new DOMException("denied", "NotAllowedError");
            }),
        });
        const btn = mount().querySelector<HTMLButtonElement>(".af-mic-btn")!;

        btn.click();

        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ message: "Microphone permission was denied." }),
            ),
        );
        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("keeps what the person types while the words keep coming", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.say("call me");
        input.value = "call me later";
        input.dispatchEvent(new Event("input"));
        socket.say("call me back");

        // The turn is ended by the button alone, so typing has to survive the
        // next reading rather than stopping the microphone.
        expect(input.value).toBe("call me back later");
    });

    it("does not fight the person over a reading they deleted", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        socket.say("wrong words");
        input.value = "never mind";
        input.dispatchEvent(new Event("input"));
        socket.say("wrong words again");

        expect(input.value).toBe("never mind");
    });

    it("stops rebasing once the turn is over", async () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        btn.click();
        socket.settle("all done");
        input.value = "all done and then some";
        input.dispatchEvent(new Event("input"));

        // Nothing is listening to the field any more, so the message is theirs.
        expect(input.value).toBe("all done and then some");
    });
});

describe("the mic button where the browser recognises for itself", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        Reflect.set(window, "isSecureContext", true);
    });

    afterEach(() => {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
    });

    it("puts what the browser heard into the composer", async () => {
        const mod = await import("../ext/stt");
        vi.spyOn(mod, "recognisesHere").mockReturnValue(true);
        let heard: ((e: unknown) => void) | null = null;
        let ended: (() => void) | null = null;
        Reflect.set(
            window,
            "webkitSpeechRecognition",
            class {
                continuous = false;
                interimResults = false;
                lang = "";
                set onresult(fn: (e: unknown) => void) {
                    heard = fn;
                }
                onerror: unknown = null;
                set onend(fn: () => void) {
                    ended = fn;
                }
                start(): void {}
                stop(): void {}
                abort(): void {}
                addEventListener(): void {}
                removeEventListener(): void {}
                dispatchEvent(): boolean {
                    return true;
                }
            },
        );

        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;

        btn.click();
        (heard as unknown as (e: unknown) => void)?.({
            resultIndex: 0,
            results: { length: 1, 0: { isFinal: false, length: 1, 0: { transcript: "hello there" } } },
        });

        // No audio leaves for this deployment on that branch: the browser hands
        // back words, and the composer takes them like any other reading.
        expect(input.value).toBe("hello there");

        // The recogniser outlives the test: it is one object for the page, and a
        // turn left open decides whichever test runs next.
        (ended as unknown as () => void)?.();
    });
});

describe("ending a turn at the browser's own recogniser", () => {
    const made: { it: any } = { it: null };

    beforeEach(async () => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        Reflect.set(window, "isSecureContext", true);
        const mod = await import("../ext/stt");
        vi.spyOn(mod, "recognisesHere").mockReturnValue(true);
        Reflect.set(
            window,
            "webkitSpeechRecognition",
            class {
                continuous = false;
                interimResults = false;
                lang = "";
                onresult: unknown = null;
                onerror: ((e: { error?: string }) => void) | null = null;
                onend: (() => void) | null = null;
                stopped = 0;
                constructor() {
                    made.it = this;
                }
                start(): void {}
                stop(): void {
                    // A real one always ends the turn after being stopped, and a
                    // fake that does not leaves the next turn thinking it is
                    // still listening.
                    this.stopped += 1;
                    this.onend?.();
                }
                abort(): void {}
                addEventListener(): void {}
                removeEventListener(): void {}
                dispatchEvent(): boolean {
                    return true;
                }
            },
        );
    });

    afterEach(() => {
        Reflect.deleteProperty(window, "webkitSpeechRecognition");
    });

    it("stops when the person types instead", () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        btn.click();

        input.value = "typed instead";
        input.dispatchEvent(new Event("input"));

        // This recogniser hands back the whole turn each time, so there is
        // nothing to fold an edit into: the turn ends and the words are theirs.
        expect(made.it.stopped).toBe(1);
    });

    it("unlights the button on the second click", () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        btn.click();
        expect(btn.classList.contains("recording")).toBe(true);

        btn.click();

        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("says why when the browser refuses", () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        btn.click();

        made.it.onerror?.({ error: "not-allowed" });

        expect(btn.classList.contains("recording")).toBe(false);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Voice input stopped" }));
    });

    it("says nothing when the turn simply ends", () => {
        const bar = mount();
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        btn.click();

        made.it.onend?.();

        // Nobody spoke, or they stopped deliberately. Neither is a fault.
        expect(btn.classList.contains("recording")).toBe(false);
        expect(toast.show).not.toHaveBeenCalled();
    });
});
