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
} from "../ui/dashboard/feedView";
import type { FeedItem } from "../types/feed";

function item(over: Partial<FeedItem>): FeedItem {
    return { type: "chat", label: "hi", agentName: "worker", timestamp: 1000, ...over };
}

describe("feedKey / dedupeAndSortFeed", () => {
    it("builds a stable identity key", () => {
        expect(feedKey(item({ timestamp: 5, type: "spawn", agentName: "a", label: "x" }))).toBe(
            "5|spawn|a|x",
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

    it("shows the empty state when nothing is visible", () => {
        const wrap = buildFeedView([], { hideHeartbeats: false, onToggleHeartbeats: vi.fn() });
        expect(wrap.querySelector(".af-feed-empty")!.textContent).toBe("No events yet.");
    });

    it("filters out system-agent rows", () => {
        const wrap = buildFeedView([item({ agentName: "io-agent" }), item({ agentName: "worker" })], {
            hideHeartbeats: false,
            onToggleHeartbeats: vi.fn(),
        });
        expect(wrap.querySelectorAll(".af-feed-item").length).toBe(1);
    });

    it("heartbeat toggle notifies the caller and hides heartbeat rows", () => {
        const onToggle = vi.fn();
        const wrap = buildFeedView([item({ type: "heartbeat", agentName: "worker" })], {
            hideHeartbeats: false,
            onToggleHeartbeats: onToggle,
        });
        const hbRow = wrap.querySelector<HTMLElement>(".af-feed-heartbeat")!;
        expect(hbRow.hidden).toBe(false);
        wrap.querySelector<HTMLButtonElement>(".af-mini-btn")!.click();
        expect(onToggle).toHaveBeenCalledWith(true);
        expect(hbRow.hidden).toBe(true);
    });
});

describe("appendFeedItemToView", () => {
    it("appends to the live feed and removes the empty placeholder", () => {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"><div class="af-feed-empty">No events yet.</div></div>`;
        appendFeedItemToView(root, item({ agentName: "worker" }), false);
        expect(root.querySelector(".af-feed-empty")).toBeNull();
        expect(root.querySelectorAll(".af-feed-item").length).toBe(1);
    });

    it("skips hidden (system-agent) items", () => {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"></div>`;
        appendFeedItemToView(root, item({ agentName: "monitor-agent" }), false);
        expect(root.querySelectorAll(".af-feed-item").length).toBe(0);
    });
});
