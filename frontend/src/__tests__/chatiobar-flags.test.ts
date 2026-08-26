/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Force the gated features ON for this file so the mic + paste paths (dead until
// the server names a branch and says it takes uploads) are exercised. The gates
// themselves are covered in feature-flags.test.ts and uploads-gate.test.ts.
vi.mock("../io/SpeechToText", () => {
    class SpeechToText {
        static isSupported() {
            return true;
        }
    }
    return { micOffered: () => true, SpeechToText };
});
vi.mock("../ui/dashboard/uploads", () => ({
    uploadsEnabled: () => true,
    uploadFile: vi.fn(async () => ({ id: "att-1" })),
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../io/SpeechToText";
import { uploadFile } from "../ui/dashboard/uploads";
import { toast } from "../ui/ToastManager";

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

describe("chatIobar with STT enabled — mic button", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("renders the mic button when STT is enabled and supported", () => {
        expect(mount(makeStt()).querySelector(".af-mic-btn")).not.toBeNull();
    });

    it("starts recording on first click", async () => {
        const stt = makeStt();
        const btn = mount(stt).querySelector<HTMLButtonElement>(".af-mic-btn")!;
        btn.click();
        await vi.waitFor(() => expect(stt.start).toHaveBeenCalled());
        expect(btn.classList.contains("recording")).toBe(true);
    });

    it("transcribes into the input on the second click", async () => {
        const stt = makeStt();
        const bar = mount(stt);
        const btn = bar.querySelector<HTMLButtonElement>(".af-mic-btn")!;
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        stt.recording = true; // pretend we're mid-recording
        btn.click();
        await vi.waitFor(() => expect(stt.stopAndTranscribe).toHaveBeenCalled());
        expect(input.value).toBe("hello there");
        expect(btn.classList.contains("recording")).toBe(false);
    });

    it("toasts when mic permission is denied", async () => {
        const stt = makeStt();
        stt.start.mockRejectedValueOnce(new Error("denied"));
        const btn = mount(stt).querySelector<HTMLButtonElement>(".af-mic-btn")!;
        btn.click();
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" })),
        );
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
