/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the network upload and the toast surface; keep isAccepted/humanSize/
// MAX_BYTES real so partition/validation logic is exercised for real.
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));
vi.mock("../ui/dashboard/uploads", async importOriginal => {
    const actual = await importOriginal<typeof import("../ui/dashboard/uploads")>();
    return { ...actual, uploadFile: vi.fn() };
});

import { DropZone } from "../ui/DropZone";
import { toast } from "../ui/ToastManager";
import { uploadFile, MAX_BYTES } from "../ui/dashboard/uploads";

function file(name: string, type: string, size?: number): File {
    const f = new File(["x"], name, { type });
    if (size != null) {
        Object.defineProperty(f, "size", { value: size });
    }
    return f;
}

function dragEvent(type: string, init: { types?: string[]; files?: File[] } = {}): DragEvent {
    const e = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(e, "dataTransfer", {
        value: { types: init.types ?? [], files: init.files ?? [] },
        configurable: true,
    });
    return e as DragEvent;
}

const overlay = () => document.querySelector<HTMLElement>(".af-drop-overlay");

describe("DropZone", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        document.body.innerHTML = "";
        new DropZone("/ingress");
    });

    it("builds the overlay and attaches it to the body", () => {
        expect(overlay()).not.toBeNull();
        expect(overlay()!.querySelector(".af-drop-list")).not.toBeNull();
    });

    it("reveals the overlay on dragenter carrying files", () => {
        window.dispatchEvent(dragEvent("dragenter", { types: ["Files"] }));
        expect(overlay()!.classList.contains("open")).toBe(true);
    });

    it("ignores dragenter without files", () => {
        window.dispatchEvent(dragEvent("dragenter", { types: ["text/plain"] }));
        expect(overlay()!.classList.contains("open")).toBe(false);
    });

    it("hides only when drag depth returns to zero", () => {
        window.dispatchEvent(dragEvent("dragenter", { types: ["Files"] }));
        window.dispatchEvent(dragEvent("dragenter", { types: ["Files"] }));
        window.dispatchEvent(dragEvent("dragleave"));
        expect(overlay()!.classList.contains("open")).toBe(true); // depth 1
        window.dispatchEvent(dragEvent("dragleave"));
        expect(overlay()!.classList.contains("open")).toBe(false); // depth 0
    });

    it("on drop: uploads accepted files and hides", async () => {
        (uploadFile as ReturnType<typeof vi.fn>).mockResolvedValue({ id: "1" });
        overlay()!.dispatchEvent(dragEvent("drop", { files: [file("a.png", "image/png")] }));
        await vi.waitFor(() => expect(uploadFile).toHaveBeenCalledTimes(1));
        expect(overlay()!.classList.contains("open")).toBe(false);
    });

    it("on drop: rejects unsupported types with a warning and skips upload", () => {
        overlay()!.dispatchEvent(dragEvent("drop", { files: [file("a.exe", "application/x-msdownload")] }));
        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ type: "alert-warning", title: "Some files skipped" }),
        );
        expect(uploadFile).not.toHaveBeenCalled();
    });

    it("on drop: rejects files over the size limit", () => {
        overlay()!.dispatchEvent(dragEvent("drop", { files: [file("big.png", "image/png", MAX_BYTES + 1)] }));
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-warning" }));
        expect(uploadFile).not.toHaveBeenCalled();
    });

    it("uploadFiles dispatches af-attachment-added per uploaded file", async () => {
        (uploadFile as ReturnType<typeof vi.fn>).mockResolvedValue({ id: "att-1" });
        const seen: unknown[] = [];
        document.addEventListener("af-attachment-added", (e: Event) => {
            seen.push((e as CustomEvent).detail.attachment);
        });
        const dz = new DropZone("/ingress");
        await dz.uploadFiles([file("a.png", "image/png"), file("b.png", "image/png")]);
        expect(seen).toEqual([{ id: "att-1" }, { id: "att-1" }]);
    });

    it("uploadFiles surfaces an error toast when an upload fails", async () => {
        (uploadFile as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));
        const dz = new DropZone("/ingress");
        await dz.uploadFiles([file("a.png", "image/png")]);
        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ type: "alert-error", title: "Upload failed" }),
        );
    });
});
