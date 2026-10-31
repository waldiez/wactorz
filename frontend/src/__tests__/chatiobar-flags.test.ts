/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Force uploads ON for this file so the paste and attach paths (dead until the
// server has said it takes uploads) are exercised. The gate itself is covered in
// uploads-gate.test.ts.
vi.mock("../ui/dashboard/uploads", () => ({
    uploadsEnabled: () => true,
    uploadFile: vi.fn(async () => ({ id: "att-1" })),
    ACCEPTED_MIME: ["image/"],
    ACCEPTED_EXT: [".pdf"],
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import { CommandCatalog, CommandCompletion } from "../ui/dashboard/commandCompletion";
import { uploadFile } from "../ui/dashboard/uploads";

function makeDeps(): IobarDeps {
    return {
        chatInput: {
            onChange: vi.fn(),
            onKeydown: vi.fn(),
            closePanel: vi.fn(),
            commands: new CommandCompletion(new CommandCatalog(async () => [])),
        } as unknown as ChatInput,
        target: () => "main",
        setTarget: vi.fn(),
        populateSelect: vi.fn(),
        send: vi.fn(),
        stop: vi.fn(),
    };
}

function mount(): HTMLElement {
    const bar = buildIobar(makeDeps());
    document.body.appendChild(bar);
    return bar;
}

describe("chatIobar with uploads enabled — paste", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("uploads a pasted file and emits af-attachment-added", async () => {
        const bar = mount();
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
        expect(mount().querySelector(".af-attach-btn")).not.toBeNull();
    });

    it("keeps the picker out of the button", () => {
        const bar = mount();

        // Interactive content nested inside a button is invalid markup, hidden
        // or not, so the picker is a sibling.
        expect(bar.querySelector(".af-attach-btn input")).toBeNull();
        expect(bar.querySelector('input[type="file"]')).not.toBeNull();
    });

    it("accepts several files at once", () => {
        const picker = mount().querySelector('input[type="file"]') as HTMLInputElement;

        expect(picker.multiple).toBe(true);
    });

    it("asks only for the types the server takes", () => {
        const picker = mount().querySelector('input[type="file"]') as HTMLInputElement;

        // A prefix takes a wildcard and an exact type does not: "application/pdf*"
        // is not a token any browser understands.
        expect(picker.accept).toContain("image/*");
        expect(picker.accept).toContain(".pdf");
        expect(picker.accept).not.toContain("*.");
    });

    it("uploads what was chosen and offers it as an attachment", async () => {
        const bar = mount();
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
