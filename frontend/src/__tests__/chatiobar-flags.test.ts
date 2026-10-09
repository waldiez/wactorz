/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// The browser exposes both capture paths here: Web Speech (stood in for below)
// and MediaRecorder (SpeechToText.isSupported). Which one a click uses is the
// voice-input setting plus whether the server can transcribe, both in storage.
vi.mock("../io/WebSpeech", () => {
    class WebSpeech {
        static isSupported() {
            return true;
        }
        listening = false;
        start(onText: (t: string, f: boolean) => void, onEnd: () => void, onError: (why: string) => void) {
            this.listening = true;
            session = {
                say: (text: string, isFinal: boolean) => onText(text, isFinal),
                fail: (why: string) => onError(why),
                finish: () => {
                    this.listening = false;
                    onEnd();
                },
            };
        }
        stop() {
            session?.finish();
        }
    }
    return { WebSpeech };
});
vi.mock("../io/SpeechToText", () => {
    class SpeechToText {
        static isSupported() {
            return true;
        }
    }
    return { SpeechToText };
});
vi.mock("../ui/dashboard/uploads", () => ({
    uploadsEnabled: () => true,
    uploadFile: vi.fn(async () => ({ id: "att-1" })),
    ACCEPTED_MIME: ["image/"],
    ACCEPTED_EXT: [".pdf"],
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, webSpeechErrorMessage, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../io/SpeechToText";
import { uploadFile } from "../ui/dashboard/uploads";
import { toast } from "../ui/ToastManager";
import { safeStorage } from "../safeStorage";
import { STT_AVAILABLE_KEY, VOICE_MODE_KEY, setVoiceMode, type VoiceMode } from "../io/voiceInput";

/** Handle on the recogniser the bar built, so a test can speak through it. */
let session: {
    say: (t: string, f: boolean) => void;
    fail: (why: string) => void;
    finish: () => void;
} | null = null;

interface FakeStt {
    start: ReturnType<typeof vi.fn>;
    stopAndTranscribe: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
}

function makeStt(): FakeStt {
    return {
        start: vi.fn(async () => {}),
        stopAndTranscribe: vi.fn(async () => "hello there"),
        cancel: vi.fn(),
    };
}

function makeDeps(stt: FakeStt): IobarDeps {
    return {
        chatInput: { onChange: vi.fn(), onKeydown: vi.fn(), closePanel: vi.fn() } as unknown as ChatInput,
        stt: stt as unknown as SpeechToText,
        target: () => "main",
        setTarget: vi.fn(),
        populateSelect: vi.fn(),
        send: vi.fn(),
        stop: vi.fn(),
    };
}

function mount(stt: FakeStt): HTMLElement {
    const bar = buildIobar(makeDeps(stt));
    document.body.appendChild(bar);
    return bar;
}

/** Choose a voice-input mode and say whether the server can transcribe. */
function configure(mode: VoiceMode, serverAvailable: boolean): void {
    safeStorage.set(VOICE_MODE_KEY, mode);
    safeStorage.set(STT_AVAILABLE_KEY, serverAvailable ? "1" : "0");
}

function parts(bar: HTMLElement): { btn: HTMLButtonElement; input: HTMLTextAreaElement } {
    return {
        btn: bar.querySelector<HTMLButtonElement>(".af-mic-btn")!,
        input: bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!,
    };
}

function resetMic(): void {
    document.body.innerHTML = "";
    session?.finish();
    session = null;
    vi.clearAllMocks();
}

describe("chatIobar mic: transcribed on the server", () => {
    beforeEach(() => {
        resetMic();
        configure("server", true);
    });

    it("records on the first click", async () => {
        const stt = makeStt();
        const { btn } = parts(mount(stt));
        btn.click();
        await vi.waitFor(() => expect(stt.start).toHaveBeenCalled());
        expect(btn.classList.contains("recording")).toBe(true);
    });

    it("transcribes into the input on the second click, after what was typed", async () => {
        const stt = makeStt();
        const { btn, input } = parts(mount(stt));
        input.value = "note:";
        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        btn.click();
        await vi.waitFor(() => expect(stt.stopAndTranscribe).toHaveBeenCalled());
        await vi.waitFor(() => expect(input.value).toBe("note: hello there"));
        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("says why when transcription fails", async () => {
        const stt = makeStt();
        stt.stopAndTranscribe.mockRejectedValueOnce(new Error("set DEEPGRAM_API_KEY"));
        const { btn } = parts(mount(stt));
        btn.click();
        await vi.waitFor(() => expect(btn.classList.contains("recording")).toBe(true));
        btn.click();
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ title: "Transcription failed", message: "set DEEPGRAM_API_KEY" }),
            ),
        );
    });

    it("toasts when mic permission is denied, and can try again", async () => {
        const stt = makeStt();
        stt.start.mockRejectedValueOnce(new Error("denied"));
        const { btn } = parts(mount(stt));
        btn.click();
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" })),
        );
        btn.click();
        await vi.waitFor(() => expect(stt.start).toHaveBeenCalledTimes(2));
    });

    it("explains instead of recording when the server cannot transcribe", () => {
        configure("server", false);
        const stt = makeStt();
        parts(mount(stt)).btn.click();
        expect(stt.start).not.toHaveBeenCalled();
        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({
                type: "alert-warning",
                message: expect.stringContaining("DEEPGRAM_API_KEY"),
            }),
        );
    });
});

describe("chatIobar mic: the browser's own recognition", () => {
    beforeEach(() => {
        resetMic();
        configure("browser", true);
    });

    it("shows a hypothesis while it is still being spoken, replacing the last one", () => {
        const { btn, input } = parts(mount(makeStt()));
        btn.click();
        session?.say("hello th", false);
        expect(input.value).toBe("hello th");
        session?.say("hello there", false);
        expect(input.value).toBe("hello there");
    });

    it("keeps what was already typed", () => {
        const { btn, input } = parts(mount(makeStt()));
        input.value = "note:";
        btn.click();
        session?.say("hello", true);
        expect(input.value).toBe("note: hello");
    });

    it("stops listening on a second click", () => {
        const { btn } = parts(mount(makeStt()));
        btn.click();
        expect(btn.classList.contains("recording")).toBe(true);
        btn.click();
        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("never records for the server in this mode", () => {
        const stt = makeStt();
        parts(mount(stt)).btn.click();
        expect(stt.start).not.toHaveBeenCalled();
    });

    it("explains a blocked microphone, and stays quiet about silence", () => {
        const { btn } = parts(mount(makeStt()));
        btn.click();
        session?.fail("no-speech");
        expect(toast.show).not.toHaveBeenCalled();
        session?.fail("not-allowed");
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Voice input" }));
    });
});

describe("chatIobar mic: auto and off", () => {
    beforeEach(resetMic);

    it("auto uses the server when it can transcribe", async () => {
        configure("auto", true);
        const stt = makeStt();
        parts(mount(stt)).btn.click();
        await vi.waitFor(() => expect(stt.start).toHaveBeenCalled());
        expect(session).toBeNull();
    });

    it("auto falls back to the browser when the server cannot", () => {
        configure("auto", false);
        const stt = makeStt();
        parts(mount(stt)).btn.click();
        expect(stt.start).not.toHaveBeenCalled();
        expect(session).not.toBeNull();
    });

    it("off hides the mic, and choosing a mode brings it back at once", () => {
        configure("off", true);
        const { btn } = parts(mount(makeStt()));
        expect(btn.hidden).toBe(true);
        setVoiceMode("auto");
        expect(btn.hidden).toBe(false);
    });
});

describe("webSpeechErrorMessage", () => {
    it("words the errors a person can act on and drops the rest", () => {
        expect(webSpeechErrorMessage("aborted")).toBeNull();
        expect(webSpeechErrorMessage("network")).toContain("could not be reached");
        expect(webSpeechErrorMessage("language-not-supported")).toContain("language-not-supported");
    });
});

describe("chatIobar with uploads enabled — paste", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("uploads a pasted file and emits af-attachment-added", async () => {
        const bar = mount(makeStt());
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        const seen: unknown[] = [];
        document.addEventListener("af-attachment-added", e =>
            seen.push((e as CustomEvent).detail.attachment),
        );

        const e = new Event("paste", { bubbles: true });
        Object.defineProperty(e, "clipboardData", {
            value: { files: [new File(["x"], "shot.png", { type: "image/png" })] },
        });
        input.dispatchEvent(e);

        await vi.waitFor(() => expect(uploadFile).toHaveBeenCalled());
        expect(seen).toEqual([{ id: "att-1" }]);
    });
});

describe("chatIobar — the attach button", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("offers one where the server takes uploads", () => {
        expect(mount(makeStt()).querySelector(".af-attach-btn")).not.toBeNull();
    });

    it("keeps the picker out of the button", () => {
        const bar = mount(makeStt());

        // Interactive content nested inside a button is invalid markup, hidden
        // or not, so the picker is a sibling.
        expect(bar.querySelector(".af-attach-btn input")).toBeNull();
        expect(bar.querySelector('input[type="file"]')).not.toBeNull();
    });

    it("accepts several files at once", () => {
        const picker = mount(makeStt()).querySelector('input[type="file"]') as HTMLInputElement;

        expect(picker.multiple).toBe(true);
    });

    it("asks only for the types the server takes", () => {
        const picker = mount(makeStt()).querySelector('input[type="file"]') as HTMLInputElement;

        // A prefix takes a wildcard and an exact type does not: "application/pdf*"
        // is not a token any browser understands.
        expect(picker.accept).toContain("image/*");
        expect(picker.accept).toContain(".pdf");
        expect(picker.accept).not.toContain("*.");
    });

    it("uploads what was chosen and offers it as an attachment", async () => {
        const bar = mount(makeStt());
        const picker = bar.querySelector('input[type="file"]') as HTMLInputElement;
        const seen: unknown[] = [];
        document.addEventListener("af-attachment-added", e =>
            seen.push((e as CustomEvent).detail?.attachment),
        );

        const file = new File(["hi"], "note.txt", { type: "text/plain" });
        Object.defineProperty(picker, "files", { value: [file], configurable: true });
        picker.dispatchEvent(new Event("change"));
        await new Promise(resolve => setTimeout(resolve, 0));

        expect(uploadFile).toHaveBeenCalledWith(file, "");
        expect(seen).toEqual([{ id: "att-1" }]);
    });
});
