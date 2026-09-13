/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
    feedItemEl,
    feedKey,
    dedupeAndSortFeed,
    buildFeedView,
    appendFeedItemToView,
    applyFilters,
    DEFAULT_FILTERS,
    type FeedFilters,
} from "../ui/dashboard/feedView";
import type { FeedItem } from "../types/feed";

function item(over: Partial<FeedItem>): FeedItem {
    return { type: "chat", label: "hi", agentName: "worker", timestamp: 1000, ...over };
}

describe("feedKey / dedupeAndSortFeed", () => {
    it("builds a stable identity key", () => {
        expect(feedKey(item({ timestamp: 5, type: "spawn", agentName: "a", label: "x" }))).toBe(
            "agent|5|spawn|a|x",
        );
    });

    it("drops exact duplicates and sorts chronologically", () => {
        const a = item({ timestamp: 30 });
        const b = item({ timestamp: 10 });
        const dup = item({ timestamp: 30 }); // identical to a
        const out = dedupeAndSortFeed([a, b, dup]);
        expect(out.map(i => i.timestamp)).toEqual([10, 30]);
    });
});

describe("feedItemEl", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("labels the user's own turn as 'you'", () => {
        const c = document.createElement("div");
        feedItemEl(c, item({ role: "user" }));
        const agent = c.querySelector(".af-feed-agent")!;
        expect(agent.textContent).toBe("you");
        expect(agent.classList.contains("af-feed-agent-user")).toBe(true);
    });

    it("keeps the @mention on the user's own turn as a styled token", () => {
        const c = document.createElement("div");
        feedItemEl(c, item({ role: "user", label: "@researcher find the docs" }));
        const text = c.querySelector<HTMLElement>(".af-feed-text")!;
        const mention = text.querySelector<HTMLElement>(".af-feed-mention")!;
        expect(mention.textContent).toBe("@researcher"); // routed agent as its own token
        expect(text.textContent).toBe("@researcher find the docs"); // line reads naturally
    });

    it("strips the @agent prefix and truncates long labels", () => {
        const c = document.createElement("div");
        const long = "@main " + "x".repeat(200);
        feedItemEl(c, item({ label: long }));
        const text = c.querySelector<HTMLElement>(".af-feed-text")!;
        expect(text.textContent!.startsWith("x")).toBe(true); // prefix stripped
        expect(text.textContent!.endsWith("…")).toBe(true); // truncated
        expect(text.title.length).toBeGreaterThan(120); // full label kept as tooltip
    });
});

describe("buildFeedView", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    /** Filters with heartbeats shown and both sources visible, unless overridden. */
    const shown = (over: Partial<FeedFilters> = {}): FeedFilters => ({
        ...DEFAULT_FILTERS,
        source: "all",
        hideHeartbeats: false,
        ...over,
    });

    const view = (items: Parameters<typeof buildFeedView>[0], over: Partial<FeedFilters> = {}) =>
        buildFeedView(items, { filters: shown(over), onFiltersChange: vi.fn() });

    it("shows the empty state when nothing is visible", () => {
        const wrap = view([]);
        const empty = wrap.querySelector<HTMLElement>(".af-feed-empty")!;
        expect(empty.textContent).toBe("No events yet.");
        expect(empty.hidden).toBe(false);
    });

    it("marks the feed as a polite live-region log for screen readers", () => {
        const feed = view([]).querySelector("#af-feed-view")!;
        expect(feed.getAttribute("role")).toBe("log");
        expect(feed.getAttribute("aria-live")).toBe("polite");
    });

    it("keeps what the house did when heartbeats are hidden", () => {
        // Every Home Assistant state change arrives as `health`, and hiding
        // heartbeats was hiding those too: the filter matched the row's class,
        // and `health` was painted with the heartbeat's. Turning a light on
        // reached the browser and was then dropped one step before the screen.
        const wrap = view(
            [
                item({ type: "health", agentName: "ha", label: "Kitchen → on" }),
                item({ type: "heartbeat", agentName: "worker" }),
            ],
            { hideHeartbeats: true },
        );

        const shownRows = [...wrap.querySelectorAll<HTMLElement>(".af-feed-item")].filter(row => !row.hidden);
        expect(shownRows).toHaveLength(1);
        expect(shownRows[0]!.textContent).toContain("Kitchen → on");
    });

    it("does not paint what the house did as a heartbeat", () => {
        const wrap = view([item({ type: "health", agentName: "ha", label: "Kitchen → on" })]);

        const row = wrap.querySelector<HTMLElement>(".af-feed-item")!;
        expect(row.classList.contains("af-feed-heartbeat")).toBe(false);
        expect(row.classList.contains("af-feed-health")).toBe(true);
    });

    it("still keeps heartbeats themselves out", () => {
        const wrap = view([item({ type: "heartbeat", agentName: "worker" })], {
            hideHeartbeats: true,
        });

        // Never rendered rather than rendered and hidden: `isHidden` drops these
        // before the row is built, which is the check that always read the type.
        expect(wrap.querySelectorAll(".af-feed-item")).toHaveLength(0);
    });

    it("hides a heartbeat when the toggle is flipped after the rows are drawn", () => {
        const wrap = view([
            item({ type: "heartbeat", agentName: "worker" }),
            item({ type: "health", agentName: "ha", label: "Kitchen → on" }),
        ]);

        applyFilters(wrap.querySelector("#af-feed-view")!, shown({ hideHeartbeats: true }));

        const rows = [...wrap.querySelectorAll<HTMLElement>(".af-feed-item")];
        const visible = rows.filter(row => !row.hidden);
        // This is the path that was hiding the house along with the heartbeats.
        expect(visible).toHaveLength(1);
        expect(visible[0]!.textContent).toContain("Kitchen → on");
    });

    it("filters out system-agent rows", () => {
        const wrap = view([item({ agentName: "monitor-agent" }), item({ agentName: "worker" })]);
        expect(wrap.querySelectorAll(".af-feed-item").length).toBe(1);
    });

    it("heartbeat toggle notifies the caller and hides heartbeat rows", () => {
        const onFiltersChange = vi.fn();
        const wrap = buildFeedView([item({ type: "heartbeat", agentName: "worker" })], {
            filters: shown(),
            onFiltersChange,
        });
        const hbRow = wrap.querySelector<HTMLElement>(".af-feed-heartbeat")!;
        expect(hbRow.hidden).toBe(false);

        wrap.querySelector<HTMLButtonElement>(".af-mini-btn")!.click();

        expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ hideHeartbeats: true }));
        expect(hbRow.hidden).toBe(true);
    });

    it("defaults to agents only, so the panel keeps the character it had", () => {
        expect(DEFAULT_FILTERS.source).toBe("agent");
    });
});

describe("appendFeedItemToView", () => {
    const shown = (over: Partial<FeedFilters> = {}): FeedFilters => ({
        ...DEFAULT_FILTERS,
        source: "all",
        hideHeartbeats: false,
        ...over,
    });

    it("appends to the live feed and hides the empty placeholder", () => {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"><div class="af-feed-empty">No events yet.</div></div>`;

        appendFeedItemToView(root, item({ agentName: "worker" }), shown());

        expect(root.querySelector<HTMLElement>(".af-feed-empty")!.hidden).toBe(true);
        expect(root.querySelectorAll(".af-feed-item").length).toBe(1);
    });

    it("skips hidden (system-agent) items", () => {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"></div>`;

        appendFeedItemToView(root, item({ agentName: "monitor-agent" }), shown());

        expect(root.querySelectorAll(".af-feed-item").length).toBe(0);
    });

    it("appends a row the current filters hide, but hidden", () => {
        // It must still be in the DOM: flipping the filter back has to reveal it
        // without refetching, which is the whole point of filtering in place.
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"></div>`;

        appendFeedItemToView(root, item({ agentName: "worker" }), shown({ source: "app" }));

        expect(root.querySelector<HTMLElement>(".af-feed-item")!.hidden).toBe(true);
    });
});
