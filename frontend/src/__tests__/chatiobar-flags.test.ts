/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Force the gated features ON for this file so the mic + paste paths (dead while
// STT is off and the server has not said it takes uploads) are exercised. The
// gates themselves are covered in feature-flags.test.ts and uploads-gate.test.ts.
vi.mock("../io/WebSpeech", () => {
    class WebSpeech {
        static isSupported() {
            return true;
        }
        listening = false;
        start(onText: (t: string, f: boolean) => void, onEnd: () => void) {
            this.listening = true;
            session = {
                say: (text: string, isFinal: boolean) => onText(text, isFinal),
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

/** Handle on the recogniser the bar built, so a test can speak through it. */
let session: { say: (t: string, f: boolean) => void; finish: () => void } | null = null;
vi.mock("../ui/dashboard/uploads", () => ({
    uploadsEnabled: () => true,
    uploadFile: vi.fn(async () => ({ id: "att-1" })),
    ACCEPTED_MIME: ["image/"],
    ACCEPTED_EXT: [".pdf"],
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../io/SpeechToText";
import { uploadFile } from "../ui/dashboard/uploads";

interface FakeStt {
    recording: boolean;
    start: ReturnType<typeof vi.fn>;
    stopAndTranscribe: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
}

function makeStt(): FakeStt {
    return {
        recording: false,
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

describe("chatIobar — the microphone", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        // The bar keeps one recogniser for the module, so a case that leaves it
        // listening would make the next case's click stop rather than start.
        session?.finish();
        session = null;
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("renders the button where the browser has the API", () => {
        expect(mount(makeStt()).querySelector(".af-mic-btn")).not.toBeNull();
    });

    it("shows a hypothesis while it is still being spoken", () => {
        const bar = mount(makeStt());
        const input = bar.querySelector("textarea") as HTMLTextAreaElement;
        (bar.querySelector(".af-mic-btn") as HTMLButtonElement).click();

        session?.say("hello th", false);

        // The point of this path: text appears without waiting for the end.
        expect(input.value).toBe("hello th");
    });

    it("replaces a hypothesis rather than appending to it", () => {
        const bar = mount(makeStt());
        const input = bar.querySelector("textarea") as HTMLTextAreaElement;
        (bar.querySelector(".af-mic-btn") as HTMLButtonElement).click();

        session?.say("hello th", false);
        session?.say("hello there", false);

        expect(input.value).toBe("hello there");
    });

    it("keeps what was already typed", () => {
        const bar = mount(makeStt());
        const input = bar.querySelector("textarea") as HTMLTextAreaElement;
        input.value = "note:";
        (bar.querySelector(".af-mic-btn") as HTMLButtonElement).click();

        session?.say("hello", true);

        expect(input.value).toBe("note: hello");
    });

    it("stops listening on a second click", () => {
        const bar = mount(makeStt());
        const btn = bar.querySelector(".af-mic-btn") as HTMLButtonElement;
        btn.click();
        expect(btn.classList.contains("recording")).toBe(true);

        btn.click();

        expect(btn.classList.contains("recording")).toBe(false);
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
