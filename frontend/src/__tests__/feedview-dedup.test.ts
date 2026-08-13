/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { dedupeAndSortFeed, feedKey } from "../ui/dashboard/feedView";
import type { AppLogItem, FeedItem } from "../types/feed";

// The in-card feed is fed from several unsynchronised sources (SQLite seed, WS
// log_feed replay, live chat) in arrival order and without ids. These tests
// lock the content-dedup + chronological-sort behaviour that keeps it stable.

function item(over: Partial<FeedItem> = {}): FeedItem {
    return { type: "chat", label: "hi", agentName: "main", timestamp: 1000, ...over };
}

describe("dedupeAndSortFeed", () => {
    it("drops exact-duplicate events (same time/type/agent/label)", () => {
        const out = dedupeAndSortFeed([item(), item(), item()]);
        expect(out).toHaveLength(1);
    });

    it("keeps events that differ in any identity field", () => {
        const out = dedupeAndSortFeed([
            item({ label: "a" }),
            item({ label: "b" }),
            item({ agentName: "io", label: "a" }),
            item({ timestamp: 2000, label: "a" }),
        ]);
        expect(out).toHaveLength(4);
    });

    it("sorts by timestamp ascending regardless of arrival order", () => {
        const out = dedupeAndSortFeed([
            item({ timestamp: 3000, label: "c" }),
            item({ timestamp: 1000, label: "a" }),
            item({ timestamp: 2000, label: "b" }),
        ]);
        expect(out.map(i => i.label)).toEqual(["a", "b", "c"]);
    });

    it("feedKey distinguishes items by their identity fields", () => {
        expect(feedKey(item())).toBe(feedKey(item()));
        expect(feedKey(item({ label: "x" }))).not.toBe(feedKey(item({ label: "y" })));
    });
});

describe("entries from both sources together", () => {
    const log = (over: Partial<AppLogItem> = {}): AppLogItem => ({
        source: "app",
        ts: 1_700_000_000,
        level: "INFO",
        origin: "wactorz.web.app",
        text: "listening on 0.0.0.0:8888",
        ...over,
    });
    const agentItem = (over: Partial<FeedItem> = {}): FeedItem => ({
        type: "spawn",
        label: "spawned",
        agentName: "worker",
        timestamp: 1_700_000_000_000,
        ...over,
    });

    it("interleaves the two chronologically", () => {
        // ⚠ The units differ as well as the field name: agent events are in
        // milliseconds, the log buffer records seconds. Sorted naively, every
        // log row lands in 1970 and the feed reads as if nothing was logged.
        const sorted = dedupeAndSortFeed([
            agentItem({ timestamp: 1_700_000_002_000, label: "second" }),
            log({ ts: 1_700_000_001, text: "first" }),
        ]);

        expect(sorted.map(e => ("text" in e ? e.text : e.label))).toEqual(["first", "second"]);
    });

    it("keeps two entries that differ only by source", () => {
        const same = dedupeAndSortFeed([
            log({ ts: 1_700_000_000, text: "same words" }),
            agentItem({ timestamp: 1_700_000_000_000, label: "same words" }),
        ]);

        expect(same).toHaveLength(2);
    });

    it("still drops a genuine duplicate log record", () => {
        expect(dedupeAndSortFeed([log(), log()])).toHaveLength(1);
    });
});
