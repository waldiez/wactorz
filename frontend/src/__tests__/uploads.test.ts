/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { isAccepted, humanSize, isImage, uploadFile } from "../ui/dashboard/uploads";

function file(name: string, type: string): File {
    return new File([new Uint8Array(8)], name, { type });
}

describe("uploads", () => {
    it("accepts images / pdf / text by MIME prefix", () => {
        expect(isAccepted(file("a.png", "image/png"))).toBe(true);
        expect(isAccepted(file("a.pdf", "application/pdf"))).toBe(true);
        expect(isAccepted(file("a.txt", "text/plain"))).toBe(true);
    });

    it("accepts known extensions when the MIME is blank", () => {
        expect(isAccepted(file("a.csv", ""))).toBe(true);
        expect(isAccepted(file("a.docx", ""))).toBe(true);
    });

    it("rejects unsupported types", () => {
        expect(isAccepted(file("a.exe", "application/x-msdownload"))).toBe(false);
    });

    it("humanSize formats B / KB / MB", () => {
        expect(humanSize(512)).toBe("512 B");
        expect(humanSize(2048)).toBe("2 KB");
        expect(humanSize(5 * 1024 * 1024)).toBe("5.0 MB");
    });

    it("isImage is true only for image MIME types", () => {
        expect(isImage({ id: "x", name: "a.png", mime: "image/png", size: 1 })).toBe(true);
        expect(isImage({ id: "x", name: "a.pdf", mime: "application/pdf", size: 1 })).toBe(false);
    });

    it("uploadFile rejects an unsupported type", async () => {
        await expect(uploadFile(file("a.exe", "application/x-msdownload"))).rejects.toThrow();
    });

    it("uploadFile POSTs to /api/upload and maps the server response", async () => {
        const orig = globalThis.fetch;
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ id: "srv-1", name: "shot.png", mime: "image/png", size: 8 }),
        })) as unknown as typeof fetch;
        const att = await uploadFile(file("shot.png", "image/png"), "/ingress");
        expect(att.id).toBe("srv-1");
        expect(att.url).toBe("/ingress/api/upload/srv-1");
        globalThis.fetch = orig;
    });

    it("uploadFile rejects when the upload endpoint errors", async () => {
        const orig = globalThis.fetch;
        globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500 })) as unknown as typeof fetch;
        await expect(uploadFile(file("shot.png", "image/png"))).rejects.toThrow();
        globalThis.fetch = orig;
    });
});
