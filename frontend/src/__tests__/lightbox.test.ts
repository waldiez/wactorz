/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { openLightbox } from "../ui/dashboard/lightbox";

describe("openLightbox", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("appends a lightbox overlay with the given image url and alt", () => {
        openLightbox("http://example.com/a.png", "a picture");
        const overlay = document.querySelector(".af-lightbox");
        expect(overlay).not.toBeNull();
        const img = overlay!.querySelector("img")!;
        expect(img.getAttribute("src")).toBe("http://example.com/a.png");
        expect(img.getAttribute("alt")).toBe("a picture");
        expect(overlay!.getAttribute("role")).toBe("dialog");
        expect(overlay!.getAttribute("aria-modal")).toBe("true");
        expect(overlay!.getAttribute("aria-label")).toBe("a picture");
    });

    it("falls back to a generic aria-label when alt is empty", () => {
        openLightbox("http://example.com/a.png");
        const overlay = document.querySelector(".af-lightbox")!;
        expect(overlay.getAttribute("aria-label")).toBe("Image preview");
    });

    it("defaults alt to an empty string", () => {
        openLightbox("http://example.com/a.png");
        const img = document.querySelector(".af-lightbox img")!;
        expect(img.getAttribute("alt")).toBe("");
    });

    it("keeps only one instance open at a time", () => {
        openLightbox("http://example.com/a.png");
        openLightbox("http://example.com/b.png");
        expect(document.querySelectorAll(".af-lightbox").length).toBe(1);
        const img = document.querySelector(".af-lightbox img")!;
        expect(img.getAttribute("src")).toBe("http://example.com/b.png");
    });

    it("does not leave the replaced instance listening on document", () => {
        openLightbox("http://example.com/a.png");
        openLightbox("http://example.com/b.png");

        // one Escape must close the one that is open, not merely the stale one
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        expect(document.querySelector(".af-lightbox")).toBeNull();
    });

    it("closes when the overlay is clicked", () => {
        openLightbox("http://example.com/a.png");
        const overlay = document.querySelector<HTMLElement>(".af-lightbox")!;
        overlay.click();
        expect(document.querySelector(".af-lightbox")).toBeNull();
    });

    it("still closes on a click on the image itself", () => {
        openLightbox("http://example.com/a.png");
        document.querySelector<HTMLElement>(".af-lightbox img")!.click();
        expect(document.querySelector(".af-lightbox")).toBeNull();
    });

    it("closes on Escape and removes the keydown listener", () => {
        openLightbox("http://example.com/a.png");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        expect(document.querySelector(".af-lightbox")).toBeNull();
        // A second Escape after close must be a no-op (listener was removed).
        expect(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))).not.toThrow();
    });

    it("ignores non-Escape keys", () => {
        openLightbox("http://example.com/a.png");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });

    describe("keyboard access", () => {
        function invoker(): HTMLButtonElement {
            const btn = document.createElement("button");
            document.body.appendChild(btn);
            btn.focus();
            return btn;
        }

        it("offers a close control, not only Escape and a backdrop click", () => {
            openLightbox("http://example.com/a.png");
            const closeBtn = document.querySelector<HTMLButtonElement>(".af-lightbox button");
            expect(closeBtn).not.toBeNull();
            expect(closeBtn!.getAttribute("aria-label")).toBeTruthy();
        });

        it("closes from the close control", () => {
            openLightbox("http://example.com/a.png");
            document.querySelector<HTMLButtonElement>(".af-lightbox button")!.click();
            expect(document.querySelector(".af-lightbox")).toBeNull();
        });

        it("moves focus into the dialog on open", () => {
            invoker();
            openLightbox("http://example.com/a.png");
            const overlay = document.querySelector<HTMLElement>(".af-lightbox")!;
            expect(overlay.contains(document.activeElement)).toBe(true);
        });

        it("returns focus to whatever opened it", () => {
            const btn = invoker();
            openLightbox("http://example.com/a.png");
            document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
            expect(document.activeElement).toBe(btn);
        });

        it("keeps Tab inside the dialog", () => {
            invoker();
            openLightbox("http://example.com/a.png");
            const overlay = document.querySelector<HTMLElement>(".af-lightbox")!;

            for (let i = 0; i < 4; i++) {
                document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
                expect(overlay.contains(document.activeElement)).toBe(true);
            }
        });

        it("keeps Shift+Tab inside the dialog", () => {
            invoker();
            openLightbox("http://example.com/a.png");
            const overlay = document.querySelector<HTMLElement>(".af-lightbox")!;

            for (let i = 0; i < 4; i++) {
                document.dispatchEvent(
                    new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }),
                );
                expect(overlay.contains(document.activeElement)).toBe(true);
            }
        });

        it("lets Tab through if the dialog somehow has nothing focusable", () => {
            openLightbox("http://example.com/a.png");
            document.querySelector(".af-lightbox button")!.remove();

            const e = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
            document.dispatchEvent(e);

            // nothing to trap onto, so don't swallow the key and strand the user
            expect(e.defaultPrevented).toBe(false);
        });

        it("survives being opened with nothing focused", () => {
            (document.activeElement as HTMLElement | null)?.blur();
            openLightbox("http://example.com/a.png");
            expect(() =>
                document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })),
            ).not.toThrow();
            expect(document.querySelector(".af-lightbox")).toBeNull();
        });
    });
});
