/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderAttachTray } from "../ui/dashboard/attachTray";
import type { Attachment } from "../types/agent";

function att(id: string, name: string, mime: string): Attachment {
    return { id, name, mime, size: 1 };
}

function rootWithTray(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML = `<div id="af-attach-tray"></div>`;
    return root;
}

describe("renderAttachTray", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("no-ops when the tray element is missing", () => {
        const root = document.createElement("div");
        expect(() => renderAttachTray(root, [att("1", "a.png", "image/png")], vi.fn())).not.toThrow();
    });

    it("labels images as 'Image #N' and other files by name", () => {
        const root = rootWithTray();
        renderAttachTray(
            root,
            [att("1", "shot.png", "image/png"), att("2", "doc.pdf", "application/pdf")],
            vi.fn(),
        );
        const labels = [...root.querySelectorAll(".af-attach-chip-label")].map(e => e.textContent);
        expect(labels).toEqual(["Image #1", "doc.pdf"]);
    });

    it("calls onRemove with the attachment when × is clicked", () => {
        const root = rootWithTray();
        const onRemove = vi.fn();
        const a = att("1", "doc.pdf", "application/pdf");
        renderAttachTray(root, [a], onRemove);
        root.querySelector<HTMLButtonElement>(".af-attach-chip-x")!.click();
        expect(onRemove).toHaveBeenCalledWith(a);
    });

    it("clears previous chips on re-render", () => {
        const root = rootWithTray();
        renderAttachTray(root, [att("1", "a.pdf", "application/pdf")], vi.fn());
        renderAttachTray(root, [att("2", "b.pdf", "application/pdf")], vi.fn());
        const chips = root.querySelectorAll(".af-attach-chip");
        expect(chips.length).toBe(1);
        expect(root.querySelector(".af-attach-chip-label")!.textContent).toBe("b.pdf");
    });
});
