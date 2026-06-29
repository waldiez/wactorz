/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { selectLogFeedReplay, type LogFeedReplayState } from "../agents/mapping";
import type { LogFeedItem } from "../types/ws";

// `spawned` always maps to a feed item, so labels/order are easy to assert.
function spawn(name: string, ts: number): LogFeedItem {
    return { type: "spawned", name, agentName: name, timestamp: ts };
}

function freshState(): LogFeedReplayState {
    return { maxTs: 0, initialized: false };
}

describe("selectLogFeedReplay", () => {
    it("first batch replays the whole backlog oldest-first and sets the high-water mark", () => {
        const state = freshState();
        // Server sends newest-first; replay should come back oldest-first.
        const out = selectLogFeedReplay([spawn("c", 30), spawn("b", 20), spawn("a", 10)], state, false);
        expect(out.map(f => f.agentName)).toEqual(["a", "b", "c"]);
        expect(state.initialized).toBe(true);
        expect(state.maxTs).toBe(30);
    });

    it("first batch with no items still initializes with a zero mark", () => {
        const state = freshState();
        expect(selectLogFeedReplay([], state, false)).toEqual([]);
        expect(state).toEqual({ initialized: true, maxTs: 0 });
    });

    it("later batches emit only items newer than the high-water mark", () => {
        const state: LogFeedReplayState = { maxTs: 20, initialized: true };
        const out = selectLogFeedReplay([spawn("new", 35), spawn("old", 15), spawn("dup", 20)], state, false);
        expect(out.map(f => f.agentName)).toEqual(["new"]);
        expect(state.maxTs).toBe(35);
    });

    it("advances the mark but pushes nothing while direct MQTT is live", () => {
        const state: LogFeedReplayState = { maxTs: 20, initialized: true };
        const out = selectLogFeedReplay([spawn("x", 40)], state, true);
        expect(out).toEqual([]);
        // Mark MUST still advance, so a later reconnect doesn't replay the backlog.
        expect(state.maxTs).toBe(40);
    });

    it("leaves the mark unchanged when a later batch has nothing new", () => {
        const state: LogFeedReplayState = { maxTs: 50, initialized: true };
        const out = selectLogFeedReplay([spawn("stale", 10)], state, false);
        expect(out).toEqual([]);
        expect(state.maxTs).toBe(50);
    });

    it("drops entries that don't map to a feed item (e.g. unknown type)", () => {
        const state = freshState();
        const out = selectLogFeedReplay([{ type: "mystery", timestamp: 5 }, spawn("kept", 6)], state, false);
        expect(out.map(f => f.agentName)).toEqual(["kept"]);
    });
});
