/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { IOManager } from "../io/IOManager";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { WSClient } from "../io/WSClient";
import type { ServerEventRouter } from "../io/ServerEventRouter";

const agentInfo: AgentInfo = { id: "a1", name: "alpha", state: "running", protected: false };

function makeRouter(connected = true) {
    return { emit: vi.fn(() => connected) } as unknown as ServerEventRouter;
}

interface FakeWS {
    send: ReturnType<typeof vi.fn>;
    on: ReturnType<typeof vi.fn>;
    chunkCb?: (chunk: string, from: string, timestampMs?: number, agentId?: string) => void;
    endCb?: (from: string, agentId?: string) => void;
}

function makeWS(sendResult = true): FakeWS {
    const ws: FakeWS = {
        send: vi.fn(() => sendResult),
        on: vi.fn((event: string, cb: never) => {
            if (event === "streamChunk") {
                ws.chunkCb = cb;
            } else {
                ws.endCb = cb;
            }
        }),
    };
    return ws;
}

let dispatched: CustomEvent[];
const lastEvent = (type: string) => [...dispatched].reverse().find(e => e.type === type);

beforeEach(() => {
    vi.clearAllMocks();
    dispatched = [];
    vi.spyOn(document, "dispatchEvent").mockImplementation(e => {
        dispatched.push(e as CustomEvent);
        return true;
    });
});
afterEach(() => vi.restoreAllMocks());

describe("IOManager.send", () => {
    it("sends chat over the WebSocket", async () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        await io.send("hello", agentInfo, [], "m1");
        // By name and by id: the server keeps the thread by the id.
        expect(ws.send).toHaveBeenCalledWith("@alpha hello", "alpha", [], "a1", "m1");
    });

    it("mentions the agent as the page calls it: its address where the name is shared", async () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        await io.send("hello", agentInfo, [], "m1", "alpha@pi");
        await io.send("@beta hi", agentInfo, [], "m2", "alpha@pi");

        expect(ws.send.mock.calls.map(call => call[0])).toEqual(["@alpha@pi hello", "@beta hi"]);
    });

    it("leaves a slash command unaddressed, whichever thread it was typed in", async () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        await io.send("/help", agentInfo, [], "m1");
        // Addressed to the thread's agent it would reach it as the text "/help".
        expect(ws.send).toHaveBeenCalledWith("/help", "alpha", [], "a1", "m1");
    });

    it("shows a delivered message in the feed", async () => {
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS() as unknown as WSClient);
        await io.send("hello", agentInfo);
        expect(lastEvent("af-feed-push")?.detail.item).toMatchObject({
            agentName: "user",
            label: "@alpha hello",
        });
    });

    it("keeps an undelivered message out of the feed", async () => {
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS(false) as unknown as WSClient);
        await io.send("hi", null);
        // An echo before the send attempt would leave a failed message in the
        // feed, indistinguishable from one that actually went out.
        expect(lastEvent("af-feed-push")).toBeUndefined();
    });

    it("emits af-send-failed when the transport rejects the send", async () => {
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS(false) as unknown as WSClient);
        await io.send("hi", null, [], "m1");
        // under the id of the bubble rendered before the attempt, which waits to go again
        expect(lastEvent("af-send-failed")?.detail).toEqual({ id: "m1", content: "hi", target: "" });
    });
});

describe("IOManager streaming", () => {
    it("accumulates chunks and emits af-stream-end with the full text + TTS + feed", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);

        ws.chunkCb!("Hel", "alpha");
        ws.chunkCb!("lo", "alpha");
        expect(lastEvent("af-stream-chunk")?.detail).toEqual({ chunk: "lo", from: "alpha", text: "Hello" });

        ws.endCb!("alpha");
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "Hello", from: "alpha" });
        expect(lastEvent("tts-notify")?.detail).toEqual({ text: "Hello" });
        expect(lastEvent("af-feed-push")?.detail.item).toMatchObject({ agentName: "alpha", label: "Hello" });
    });

    it("emits an empty af-stream-end with no TTS/feed when nothing streamed", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        ws.endCb!("main");
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "", from: "main" });
        expect(lastEvent("tts-notify")).toBeUndefined();
        expect(lastEvent("af-feed-push")).toBeUndefined();
    });

    it("keeps the replies of two agents of one name apart, by their ids", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);

        ws.chunkCb!("sun", "weather", 1, "on-main");
        ws.chunkCb!("rain", "weather", 2, "on-pi");
        ws.chunkCb!("ny", "weather", 3, "on-main");
        ws.endCb!("weather", "on-pi");

        expect(lastEvent("af-stream-chunk")?.detail.text).toBe("sunny");
        expect(lastEvent("af-stream-end")?.detail).toEqual({
            text: "rain",
            from: "weather",
            agentId: "on-pi",
        });
    });

    it("resets the buffer between turns", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        ws.chunkCb!("one", "alpha");
        ws.endCb!("alpha");
        ws.chunkCb!("two", "beta");
        ws.endCb!("beta");
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "two", from: "beta" });
    });

    it("caps accumulation on a runaway stream instead of growing without bound", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        const chunk = "x".repeat(10_000);
        for (let i = 0; i < 30; i++) {
            ws.chunkCb!(chunk, "alpha");
        }
        ws.endCb!("alpha");
        const text = lastEvent("af-stream-end")?.detail.text as string;
        expect(text.length).toBeLessThan(300_000);
        expect(text.length).toBeGreaterThan(0);
    });
});

describe("IOManager.receiveAgentMessage", () => {
    const reply = (over: Partial<ChatMessage> = {}): ChatMessage => ({
        id: "m",
        from: "alpha",
        to: "user",
        threadId: "alpha",
        content: "hi",
        timestampMs: 1,
        ...over,
    });

    it("reads a user-directed reply aloud", () => {
        new IOManager(makeRouter()).receiveAgentMessage(reply());
        expect(lastEvent("tts-notify")?.detail).toEqual({ text: "hi", from: "alpha" });
    });

    it("ignores agent↔agent chatter", () => {
        new IOManager(makeRouter()).receiveAgentMessage(reply({ to: "beta" }));
        expect(lastEvent("tts-notify")).toBeUndefined();
    });

    it("reads a non-streamed reply aloud in direct_ws mode too", () => {
        // Regression: non-streamed direct_ws replies (slash commands, one-shot,
        // errors) arrive WS-only and must speak — streamed ones already do.
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS() as unknown as WSClient);
        io.receiveAgentMessage(reply());
        expect(lastEvent("tts-notify")?.detail).toEqual({ text: "hi", from: "alpha" });
    });
});

describe("IOManager.send — attachments", () => {
    it("carries attachment ids onto the wire", async () => {
        // The composer resolves files to ids before sending; only ids travel,
        // because the server holds the record of what each one actually is.
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);

        await io.send("look at this", agentInfo, ["f1", "f2"], "m1");

        expect(ws.send).toHaveBeenCalledWith("@alpha look at this", "alpha", ["f1", "f2"], "a1", "m1");
    });
});
