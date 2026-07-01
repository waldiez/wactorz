/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createHaFeedPusher, parseHaRawEvent } from "../ui/haFeed";
import type { FeedItem } from "../types/feed";

// A realistic epoch base: the pusher compares `now - (lastSeen ?? 0)`, so a
// near-zero clock would falsely dedup the very first event. Real Date.now() is
// always far larger than the 5s window, so use a real-world timestamp here.
const BASE = 1_700_000_000_000;

describe("createHaFeedPusher", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(BASE);
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    it("pushes a health item for a feed-worthy domain", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("light.kitchen", "on", "Kitchen");
        expect(push).toHaveBeenCalledTimes(1);
        expect(push.mock.calls[0]![0]).toMatchObject({
            type: "health",
            label: "Kitchen → on",
            agentName: "ha",
            timestamp: BASE,
        });
    });

    it("ignores domains outside the feed allow-list", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("sensor.temp", "on", "Temp");
        expect(push).not.toHaveBeenCalled();
    });

    it("ignores entity ids without a domain", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("nodomain", "on", "X");
        expect(push).not.toHaveBeenCalled();
    });

    it("skips numeric states (sensors leaking through)", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("climate.living", "23", "Living");
        expect(push).not.toHaveBeenCalled();
    });

    it("skips 'unknown' states", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("light.kitchen", "unknown", "Kitchen");
        expect(push).not.toHaveBeenCalled();
    });

    it("de-duplicates identical entity+state within the window", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        const pusher = createHaFeedPusher(push);
        pusher("light.kitchen", "on", "Kitchen");
        vi.advanceTimersByTime(4999);
        pusher("light.kitchen", "on", "Kitchen");
        expect(push).toHaveBeenCalledTimes(1);
    });

    it("allows the same entity+state again after the window elapses", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        const pusher = createHaFeedPusher(push);
        pusher("light.kitchen", "on", "Kitchen");
        vi.advanceTimersByTime(5001);
        pusher("light.kitchen", "on", "Kitchen");
        expect(push).toHaveBeenCalledTimes(2);
    });

    it("treats a different state as a distinct event (not deduped)", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        const pusher = createHaFeedPusher(push);
        pusher("light.kitchen", "on", "Kitchen");
        pusher("light.kitchen", "off", "Kitchen");
        expect(push).toHaveBeenCalledTimes(2);
    });

    it("eviction sweep keeps still-fresh entries deduped", () => {
        // A later event triggers the prune; a within-window entry must survive it
        // (guards the leak fix from over-evicting and breaking dedup).
        const push = vi.fn<(item: FeedItem) => void>();
        const pusher = createHaFeedPusher(push);
        pusher("light.a", "on", "A");
        vi.advanceTimersByTime(1000);
        pusher("light.b", "on", "B"); // triggers a sweep; A is only 1s old
        push.mockClear();
        pusher("light.a", "on", "A"); // A still inside the 5s window → deduped
        expect(push).not.toHaveBeenCalled();
    });
});

describe("parseHaRawEvent", () => {
    it("returns null for non-ha topics", () => {
        expect(parseHaRawEvent("agents/x/heartbeat", {})).toBeNull();
    });

    it("returns null when there is no state", () => {
        expect(parseHaRawEvent("ha/state/light/k", { new_state: {} })).toBeNull();
        expect(parseHaRawEvent("ha/state/light/k", {})).toBeNull();
    });

    it("parses entity_id, state and friendly_name", () => {
        const ev = parseHaRawEvent("ha/state/light/k", {
            entity_id: "light.kitchen",
            new_state: { state: "on", attributes: { friendly_name: "Kitchen" } },
        });
        expect(ev).toEqual({ entityId: "light.kitchen", state: "on", friendlyName: "Kitchen" });
    });

    it("falls back: entity id from topic tail, friendly name from entity id", () => {
        const ev = parseHaRawEvent("ha/state/light/k", { new_state: { state: "off" } });
        expect(ev).toEqual({ entityId: "light.k", state: "off", friendlyName: "light.k" });
    });

    it("tolerates a null payload", () => {
        expect(parseHaRawEvent("ha/state/light/k", null)).toBeNull();
    });
});
