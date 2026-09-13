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

    it("ignores the domains that are readings rather than events", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        for (const eid of ["sensor.temp", "number.target", "update.core", "sun.sun", "zone.home"]) {
            createHaFeedPusher(push)(eid, "on", "X");
        }
        expect(push).not.toHaveBeenCalled();
    });

    it("ignores a value someone stored, however much it looks like a moment", () => {
        const push = vi.fn<(item: FeedItem) => void>();

        // A date is not an event: nothing happened when this was set, and the
        // reading filter cannot tell it from one because it is not a number.
        createHaFeedPusher(push)("input_datetime.wake_up", "2026-08-31 07:00:00", "Wake up");
        createHaFeedPusher(push)("date.holiday", "2026-12-25", "Holiday");
        createHaFeedPusher(push)("time.alarm", "07:00:00", "Alarm");

        expect(push).not.toHaveBeenCalled();
    });

    it("shows a domain nobody thought to name", () => {
        const push = vi.fn<(item: FeedItem) => void>();

        // The point of naming noise instead of naming what to keep: a domain
        // Home Assistant adds later arrives on its own, rather than being
        // absent with nothing to show that it is.
        createHaFeedPusher(push)("lawn_mower.rear", "mowing", "Rear mower");

        expect(push).toHaveBeenCalledTimes(1);
    });

    it("shows the sensors that report a condition", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("binary_sensor.tapo_t300_moisture", "on", "T300 Moisture");
        expect(push.mock.calls[0]![0]).toMatchObject({ label: "T300 Moisture → on" });
    });

    it("says what happened, not when, for a thing that was activated", () => {
        const push = vi.fn<(item: FeedItem) => void>();

        // These carry the moment as their state. "Pan right → 2026-08-31T12:16"
        // is not a sentence anyone wants in a feed.
        createHaFeedPusher(push)("button.pan_right", "2026-08-31T12:16:31.563494", "Pan right");
        createHaFeedPusher(push)("event.doorbell", "2026-08-31T12:16:31+00:00", "Doorbell");
        createHaFeedPusher(push)("scene.movie_night", "2026-08-31T20:00:00+00:00", "Movie night");

        expect(push.mock.calls.map(c => c[0].label)).toEqual([
            "Pan right pressed",
            "Doorbell fired",
            "Movie night activated",
        ]);
    });

    it("keeps a timestamp state out of the reading filter", () => {
        const push = vi.fn<(item: FeedItem) => void>();

        // It starts with a digit, which is all the old check looked at.
        createHaFeedPusher(push)("button.pan_right", "2026-08-31T12:16:31.563494", "Pan right");

        expect(push).toHaveBeenCalledTimes(1);
    });

    it("ignores a thing whose state nobody knows", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("button.favorite", "unavailable", "Favorite");
        expect(push).not.toHaveBeenCalled();
    });

    it("ignores entity ids without a domain", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("nodomain", "on", "X");
        expect(push).not.toHaveBeenCalled();
    });

    it("skips readings, whatever reports them", () => {
        const push = vi.fn<(item: FeedItem) => void>();
        createHaFeedPusher(push)("climate.living", "23", "Living");
        createHaFeedPusher(push)("climate.living", "21.5", "Living");
        createHaFeedPusher(push)("climate.living", "-3,5", "Living");
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
        expect(parseHaRawEvent("homeassistant/state_changes/light/k", { new_state: {} })).toBeNull();
        expect(parseHaRawEvent("homeassistant/state_changes/light/k", {})).toBeNull();
    });

    it("parses entity_id, state and friendly_name", () => {
        const ev = parseHaRawEvent("homeassistant/state_changes/light/k", {
            entity_id: "light.kitchen",
            new_state: { state: "on", attributes: { friendly_name: "Kitchen" } },
        });
        expect(ev).toEqual({ entityId: "light.kitchen", state: "on", friendlyName: "Kitchen" });
    });

    it("falls back: entity id from topic tail, friendly name from entity id", () => {
        const ev = parseHaRawEvent("homeassistant/state_changes/light/k", { new_state: { state: "off" } });
        expect(ev).toEqual({ entityId: "light.k", state: "off", friendlyName: "light.k" });
    });

    it("parses the flat (per_entity=0) topic via the payload entity_id", () => {
        const ev = parseHaRawEvent("homeassistant/state_changes", {
            entity_id: "switch.fan",
            new_state: { state: "on" },
        });
        expect(ev).toEqual({ entityId: "switch.fan", state: "on", friendlyName: "switch.fan" });
    });

    it("tolerates a null payload", () => {
        expect(parseHaRawEvent("homeassistant/state_changes/light/k", null)).toBeNull();
    });
});
