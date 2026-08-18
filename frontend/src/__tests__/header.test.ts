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

import {
    buildHeader,
    buildBottomNav,
    setHaNavUrl,
    resolveHaNavUrl,
    releaseHeaderPopovers,
    releaseBottomNav,
} from "../ui/dashboard/header";

describe("buildHeader", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the logo/title, connection badge and the view tabs", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
        expect(header.querySelector(".af-title")!.textContent).toBe("Wactorz");
        expect(header.querySelector(".af-conn-badge")!.classList.contains("af-conn-live")).toBe(true);
        // 4 built-in view tabs + the Devices link + audio, reset and sign-out
        // icon buttons. Devices and sign-out are always built and hidden until
        // /api/config says otherwise, so both are here whatever it will answer.
        expect(header.querySelectorAll(".af-view-btn").length).toBe(8);
    });

    it("marks the current view active (class + aria-current)", () => {
        const header = buildHeader({
            view: "feed",
            connState: "demo",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
        const active = header.querySelector(".af-view-btn.active")!;
        expect(active.getAttribute("data-view")).toBe("feed");
        expect(active.getAttribute("aria-current")).toBe("page");
        expect(header.querySelectorAll('[aria-current="page"]').length).toBe(1);
    });

    it("routes a tab click through onSetView", () => {
        const onSetView = vi.fn();
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView,
            haUrl: null,
            extraViews: [],
        });
        header.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the audio icon button toggles its popover open, then closed on a second click", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
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

    it("Escape closes an open popover and returns focus to its button", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
        document.body.appendChild(header);
        const audioBtn = header.querySelector<HTMLButtonElement>('[title="Audio settings"]')!;
        audioBtn.click(); // open
        expect(document.querySelector("div.open")).not.toBeNull();

        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

        expect(document.querySelector("div.open")).toBeNull();
        expect(audioBtn.getAttribute("aria-expanded")).toBe("false");
        expect(document.activeElement).toBe(audioBtn);
    });

    it("Escape closes the More sheet and returns focus to the More button", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn(), haUrl: null, extraViews: [] });
        document.body.appendChild(nav);
        const more = nav.querySelector<HTMLButtonElement>(".af-bottom-more-btn")!;
        const sheet = nav.querySelector<HTMLElement>(".af-bottom-sheet")!;
        more.click();
        expect(sheet.classList.contains("open")).toBe(true);

        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

        expect(sheet.classList.contains("open")).toBe(false);
        expect(more.getAttribute("aria-expanded")).toBe("false");
        expect(document.activeElement).toBe(more);
    });
});

describe("popover lifetime across nav rebuilds", () => {
    const opts = () => ({
        view: "overview" as const,
        connState: "live" as const,
        onSetView: vi.fn(),
        haUrl: null,
        extraViews: [],
    });
    const popoverCount = () => document.body.querySelectorAll("[data-af-popover]").length;

    beforeEach(() => {
        releaseHeaderPopovers();
        document.body.innerHTML = "";
    });

    it("parks its popovers on the body, outside the header", () => {
        document.body.appendChild(buildHeader(opts()));
        // this is why replacing the header alone doesn't clean them up
        expect(popoverCount()).toBeGreaterThan(0);
    });

    it("does not accumulate popovers when the header is rebuilt", () => {
        document.body.appendChild(buildHeader(opts()));
        const afterFirst = popoverCount();

        for (let i = 0; i < 3; i++) {
            releaseHeaderPopovers();
            document.body.appendChild(buildHeader(opts()));
        }

        expect(popoverCount()).toBe(afterFirst);
    });

    it("takes its document listeners back down with it", () => {
        const added = vi.spyOn(document, "addEventListener");
        const removed = vi.spyOn(document, "removeEventListener");

        document.body.appendChild(buildHeader(opts()));
        const addedClicks = added.mock.calls.filter(c => c[0] === "click").length;
        expect(addedClicks).toBeGreaterThan(0);

        releaseHeaderPopovers();
        const removedClicks = removed.mock.calls.filter(c => c[0] === "click").length;

        // every outside-click listener the header installed is accounted for
        expect(removedClicks).toBe(addedClicks);
        added.mockRestore();
        removed.mockRestore();
    });

    it("is safe to call with nothing to release", () => {
        expect(() => releaseHeaderPopovers()).not.toThrow();
        releaseHeaderPopovers();
        expect(popoverCount()).toBe(0);
    });
});

describe("Devices nav link (external HA link, no embedded client)", () => {
    it("links to the HA URL in a new tab when configured", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: "http://ha.local:8123",
            extraViews: [],
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
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
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
            extraViews: [],
        });
        expect(header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!.getAttribute("href")).toBe("#");
    });

    it("setHaNavUrl points a previously-empty link at a freshly-seeded URL", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
        setHaNavUrl(header, "https://ha.example.com");
        const link = header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!;
        expect(link.getAttribute("href")).toBe("https://ha.example.com");
        expect(link.style.display).not.toBe("none");
    });

    it("rewrites the container-internal supervisor URL to the page origin (HA add-on ingress)", () => {
        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: "http://supervisor/core",
            extraViews: [],
        });
        const link = header.querySelector<HTMLAnchorElement>(".af-ha-nav-link")!;
        expect(new URL(link.href).origin).toBe(window.location.origin);
        expect(link.style.display).not.toBe("none");
    });
});

describe("resolveHaNavUrl", () => {
    it("rewrites supervisor proxy URLs (any path/trailing slash) to the page origin", () => {
        expect(resolveHaNavUrl("http://supervisor/core")).toBe(window.location.origin);
        expect(resolveHaNavUrl("http://supervisor/core/")).toBe(window.location.origin);
        expect(resolveHaNavUrl("https://supervisor")).toBe(window.location.origin);
    });

    it("passes through normal URLs, invalid strings and null unchanged", () => {
        expect(resolveHaNavUrl("http://ha.local:8123")).toBe("http://ha.local:8123");
        expect(resolveHaNavUrl("not a url")).toBe("not a url");
        expect(resolveHaNavUrl(null)).toBeNull();
    });
});

describe("buildBottomNav", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders the primary tabs (incl. the Devices link) plus a More button", () => {
        const nav = buildBottomNav({
            view: "overview",
            onSetView: vi.fn(),
            haUrl: "http://ha.local",
            extraViews: [],
        });
        expect(nav.querySelectorAll(".af-bottom-tab:not(.af-bottom-more-btn)").length).toBeGreaterThanOrEqual(
            4,
        );
        expect(nav.querySelector(".af-bottom-more-btn")).not.toBeNull();
        expect(nav.querySelector(".af-ha-nav-link")!.getAttribute("href")).toBe("http://ha.local");
    });

    it("a primary tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView, haUrl: null, extraViews: [] });
        nav.querySelector<HTMLButtonElement>('[data-view="chat"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("chat");
    });

    it("the More button toggles the secondary sheet open", () => {
        const nav = buildBottomNav({ view: "overview", onSetView: vi.fn(), haUrl: null, extraViews: [] });
        const more = nav.querySelector<HTMLButtonElement>(".af-bottom-more-btn")!;
        const sheet = nav.querySelector<HTMLElement>(".af-bottom-sheet")!;
        more.click();
        expect(sheet.classList.contains("open")).toBe(true);
    });

    it("a secondary (settings) tab routes through onSetView", () => {
        const onSetView = vi.fn();
        const nav = buildBottomNav({ view: "overview", onSetView, haUrl: null, extraViews: [] });
        nav.querySelector<HTMLButtonElement>('[data-view="settings"]')!.click();
        expect(onSetView).toHaveBeenCalledWith("settings");
    });
});

describe("bottom nav outside-click listener", () => {
    const opts = () => ({ view: "overview" as const, onSetView: vi.fn(), haUrl: null, extraViews: [] });
    const clickCount = (calls: unknown[][]) => calls.filter(c => c[0] === "click").length;

    beforeEach(() => {
        releaseBottomNav();
        document.body.innerHTML = "";
    });

    it("keeps exactly one live outside-click listener across nav rebuilds", () => {
        const added = vi.spyOn(document, "addEventListener");
        const removed = vi.spyOn(document, "removeEventListener");
        buildBottomNav(opts());
        buildBottomNav(opts()); // _rebuildNav replaces the nav with no explicit release
        buildBottomNav(opts());
        const live = clickCount(added.mock.calls) - clickCount(removed.mock.calls);
        expect(live).toBe(1);
        added.mockRestore();
        removed.mockRestore();
    });

    it("releaseBottomNav takes the listener down, and is safe with nothing to release", () => {
        const added = vi.spyOn(document, "addEventListener");
        const removed = vi.spyOn(document, "removeEventListener");
        buildBottomNav(opts());
        releaseBottomNav();
        const live = clickCount(added.mock.calls) - clickCount(removed.mock.calls);
        expect(live).toBe(0);
        expect(() => releaseBottomNav()).not.toThrow();
        added.mockRestore();
        removed.mockRestore();
    });
});
