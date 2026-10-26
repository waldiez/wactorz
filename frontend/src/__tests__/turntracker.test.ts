/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
    TURN_IDLE_TIMEOUT_MS,
    TurnTracker,
    answererOf,
    type PendingSend,
    type TurnState,
} from "../chat/turnTracker";

function send(content: string, target = "main", id = content): PendingSend {
    return { id, content, target, attachments: [] };
}

/** A tracker with a host that records what it was told. */
function tracker(): { turns: TurnTracker; delivered: string[]; states: TurnState[] } {
    const delivered: string[] = [];
    const states: TurnState[] = [];
    const turns = new TurnTracker({
        deliver: s => delivered.push(s.id),
        changed: s => states.push(s),
    });
    return { turns, delivered, states };
}

describe("who answers", () => {
    it("is the target, or main for a command typed anywhere", () => {
        expect(answererOf("hello", "weather")).toBe("weather");
        expect(answererOf("/agents", "weather")).toBe("main");
    });
});

describe("a turn", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("goes out at once, waits on whoever answers, and ends when that agent's reply does", () => {
        const { turns, delivered, states } = tracker();

        turns.request(send("hi", "weather"));
        expect(delivered).toEqual(["hi"]);
        expect(turns.state).toEqual({ agent: "weather", queued: 0 });

        turns.ended("weather");
        expect(turns.state.agent).toBeNull();
        expect(states.map(s => s.agent)).toEqual(["weather", null]);
    });

    it("is not ended or kept alive by another agent", () => {
        const { turns } = tracker();
        turns.request(send("hi", "weather"));

        turns.ended("catalog");
        turns.chunk("catalog");
        expect(turns.state.agent).toBe("weather");

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);
        expect(turns.state.agent).toBeNull();
    });

    it("waits on main for a command typed in another agent's thread", () => {
        const { turns } = tracker();

        turns.request(send("/agents", "catalog"));
        turns.ended("catalog");

        expect(turns.state.agent).toBe("main");
        turns.ended("main");
        expect(turns.state.agent).toBeNull();
    });

    it("ends when the connection goes down, or the send could not go out", () => {
        const { turns } = tracker();
        turns.request(send("a"));
        turns.connection(false);
        expect(turns.state.agent).toBeNull();

        turns.connection(true);
        turns.request(send("b"));
        turns.failed();
        expect(turns.state.agent).toBeNull();
    });

    it("stays under way while the connection is still up", () => {
        const { turns } = tracker();
        turns.request(send("a"));

        turns.connection(true);

        expect(turns.state.agent).toBe("main");
    });

    it("is given up on after a silent stretch, but a slow living stream is not cut off", () => {
        const { turns } = tracker();
        turns.request(send("a"));

        for (let i = 0; i < 3; i++) {
            vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS - 1_000);
            turns.chunk("main");
        }
        expect(turns.state.agent).toBe("main");

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);
        expect(turns.state.agent).toBeNull();
    });
});

describe("the queue", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("holds a send made during a turn and sends it, in order, as each turn ends", () => {
        const { turns, delivered } = tracker();
        turns.request(send("first", "weather"));
        expect(turns.sendsNow()).toBe(false);

        turns.request(send("second", "catalog"));
        turns.request(send("third", "weather"));
        expect(delivered).toEqual(["first"]);
        expect(turns.state.queued).toBe(2);

        turns.ended("weather");
        expect(delivered).toEqual(["first", "second"]);
        expect(turns.state).toEqual({ agent: "catalog", queued: 1 });

        turns.ended("catalog");
        expect(delivered).toEqual(["first", "second", "third"]);
    });

    it("keeps what is sent while the connection is down, and sends it once it is back", () => {
        const { turns, delivered } = tracker();
        turns.connection(false);

        turns.request(send("offline"));
        expect(delivered).toEqual([]);
        expect(turns.state.queued).toBe(1);

        turns.connection(true);
        expect(delivered).toEqual(["offline"]);
    });

    it("puts a send that failed back first, and sends it once the socket opens again", () => {
        const { turns, delivered } = tracker();
        turns.request(send("a"));
        turns.request(send("b"));

        expect(turns.failed()?.id).toBe("a");
        expect(turns.state).toEqual({ agent: null, queued: 2 });
        // The socket is closed: nothing else is tried, so nothing else fails.
        turns.request(send("c"));
        expect(delivered).toEqual(["a"]);

        // Reopened inside the window where no "disconnected" was ever said.
        turns.connection(true);
        expect(delivered).toEqual(["a", "a"]);
        turns.ended("main");
        turns.ended("main");
        expect(delivered).toEqual(["a", "a", "b", "c"]);
    });

    it("says nothing was put back when no send of its own was under way", () => {
        const { turns } = tracker();

        expect(turns.failed()).toBeNull();
    });

    it("sends the next once a quiet turn is given up on", () => {
        const { turns, delivered } = tracker();
        turns.request(send("a"));
        turns.request(send("b"));

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);

        expect(delivered).toEqual(["a", "b"]);
    });

    it("drops everything on a clear", () => {
        const { turns, delivered } = tracker();
        turns.request(send("a"));
        turns.request(send("b"));

        turns.clear();
        turns.ended("main");

        expect(delivered).toEqual(["a"]);
        expect(turns.state).toEqual({ agent: null, queued: 0 });
        expect(turns.sendsNow()).toBe(true);
    });
});
