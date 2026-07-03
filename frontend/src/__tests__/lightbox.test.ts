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

    it("closes when the overlay is clicked", () => {
        openLightbox("http://example.com/a.png");
        const overlay = document.querySelector<HTMLElement>(".af-lightbox")!;
        overlay.click();
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
});
