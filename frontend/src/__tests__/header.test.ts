/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Isolate the header from the audio/reset popovers (and their singletons).
vi.mock("../ui/dashboard/popovers", () => ({
    buildAudioPopover: () => document.createElement("div"),
    buildResetPopover: () => document.createElement("div"),
}));

import { buildHeader, buildBottomNav, setHaNavUrl } from "../ui/dashboard/header";

describe("buildHeader", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the logo/title, connection badge and the view tabs", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn(), haUrl: null });
        expect(header.querySelector(".af-title")!.textContent).toBe("Wactorz");
        expect(header.querySelector(".af-conn-badge")!.classList.contains("af-conn-live")).toBe(true);
        // 6 view tabs (incl. Graph + Identity) + the Devices link + audio + reset icon buttons
        expect(header.querySelectorAll(".af-view-btn").length).toBe(9);
    });

    it("marks the current view active (class + aria-current)", () => {
        const header = buildHeader({ view: "feed", connState: "demo", onSetView: vi.fn(), haUrl: null });
        const active = header.querySelector(".af-view-btn.active")!;
        expect(active.getAttribute("data-view")).toBe("feed");
        expect(active.getAttribute("aria-current")).toBe("page");
        expect(header.querySelectorAll('[aria-current="page"]').length).toBe(1);
    });

    it("routes a tab click through onSetView", () => {
        const onSetView = vi.fn();
        const header = buildHeader({ view: "overview", connState: "live", onSetView, haUrl: null });
        header.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the audio icon button toggles its popover open, then closed on a second click", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn(), haUrl: null });
        document.body.appendChild(header);
        const audioBtn = header.querySelector<HTMLButtonElement>('[title="Audio settings"]')!;
        expect(audioBtn.getAttribute("aria-haspopup")).toBe("true");
        expect(audioBtn.getAttribute("aria-controls")).toBeTruthy();
        expect(audioBtn.getAttribute("aria-expanded")).toBe("false");
        audioBtn.click(); // open: toggles `.open` and positions the popover
        expect(document.querySelector("div.open")).not.toBeNull();
        expect(audioBtn.getAttribute("aria-expanded")).toBe("true");
        audioBtn.click(); // close: the else branch (onClose is undefined for audio)
        expect(document.querySelector("div.open")).toBeNull();
        expect(audioBtn.getAttribute("aria-expanded")).toBe("false");
    });
});

describe("Devices nav link (external HA link, no embedded client)", () => {
    it("links to the HA URL in a new tab when configured", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: "http://ha.local:8123",
        });
        const link = header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!;
        expect(link.tagName).toBe("A");
        expect(link.getAttribute("href")).toBe("http://ha.local:8123");
        expect(link.target).toBe("_blank");
        expect(link.rel).toBe("noopener");
        expect(link.style.display).not.toBe("none");
        // It is NOT a view button — it carries no data-view and never goes active.
        expect(link.hasAttribute("data-view")).toBe(false);
    });

    it("is hidden when no HA URL is configured", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn(), haUrl: null });
        const link = header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!;
        expect(link.hasAttribute("href")).toBe(false);
        expect(link.style.display).toBe("none");
    });

    it("collapses a non-http(s) URL to '#' (no javascript: in href)", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: "javascript:alert(1)",
        });
        expect(header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!.getAttribute("href")).toBe("#");
    });

    it("setHaNavUrl points a previously-empty link at a freshly-seeded URL", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn(), haUrl: null });
        setHaNavUrl(header, "https://ha.example.com");
        const link = header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!;
        expect(link.getAttribute("href")).toBe("https://ha.example.com");
        expect(link.style.display).not.toBe("none");
    });
});

describe("buildBottomNav", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the primary tabs (incl. the Devices link) plus a More button", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn(), haUrl: "http://ha.local" });
        expect(nav.querySelectorAll(".af-bottom-tab:not(.af-bottom-more-btn)").length).toBeGreaterThanOrEqual(
            4,
        );
        expect(nav.querySelector(".af-bottom-more-btn")).not.toBeNull();
        expect(nav.querySelector(".af-ha-nav-link")!.getAttribute("href")).toBe("http://ha.local");
    });

    it("a primary tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView, haUrl: null });
        nav.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the More button toggles the secondary sheet open", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn(), haUrl: null });
        const more = nav.querySelector<HTMLButtonElement>(".af-bottom-more-btn")!;
        const sheet = nav.querySelector<HTMLElement>(".af-bottom-sheet")!;
        more.click();
        expect(sheet.classList.contains("open")).toBe(true);
    });

    it("a secondary (settings) tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView, haUrl: null });
        nav.querySelector<HTMLButtonElement>('[data-view="settings"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("settings");
    });
});
