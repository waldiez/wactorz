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

import { buildHeader, buildBottomNav } from "../ui/dashboard/header";

describe("buildHeader", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the logo/title, connection badge and the view tabs", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn() });
        expect(header.querySelector(".af-title")!.textContent).toBe("Wactorz");
        expect(header.querySelector(".af-conn-badge")!.classList.contains("af-conn-live")).toBe(true);
        // 5 view tabs + audio + reset icon buttons
        expect(header.querySelectorAll(".af-view-btn").length).toBe(7);
    });

    it("marks the current view active", () => {
        const header = buildHeader({ view: "feed", connState: "demo", onSetView: vi.fn() });
        const active = header.querySelector(".af-view-btn.active")!;
        expect(active.getAttribute("data-view")).toBe("feed");
    });

    it("routes a tab click through onSetView", () => {
        const onSetView = vi.fn();
        const header = buildHeader({ view: "overview", connState: "live", onSetView });
        header.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the audio icon button toggles its popover open, then closed on a second click", () => {
        const header = buildHeader({ view: "overview", connState: "live", onSetView: vi.fn() });
        document.body.appendChild(header);
        const audioBtn = header.querySelector<HTMLButtonElement>('[title="Audio settings"]')!;
        audioBtn.click(); // open: toggles `.open` and positions the popover
        expect(document.querySelector("div.open")).not.toBeNull();
        audioBtn.click(); // close: the else branch (onClose is undefined for audio)
        expect(document.querySelector("div.open")).toBeNull();
    });
});

describe("buildBottomNav", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the four primary tabs plus a More button", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn() });
        expect(nav.querySelectorAll(".af-bottom-tab:not(.af-bottom-more-btn)").length).toBeGreaterThanOrEqual(
            4,
        );
        expect(nav.querySelector(".af-bottom-more-btn")).not.toBeNull();
    });

    it("a primary tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView });
        nav.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the More button toggles the secondary sheet open", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn() });
        const more = nav.querySelector<HTMLButtonElement>(".af-bottom-more-btn")!;
        const sheet = nav.querySelector<HTMLElement>(".af-bottom-sheet")!;
        more.click();
        expect(sheet.classList.contains("open")).toBe(true);
    });

    it("a secondary (settings) tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView });
        nav.querySelector<HTMLButtonElement>('[data-view="settings"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("settings");
    });
});
