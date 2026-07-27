/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));
vi.mock("../ext/tts", () => ({ tts: { notify: vi.fn() } }));

import { IOManager } from "../io/IOManager";
import { toast } from "../ui/ToastManager";
import { tts } from "../ext/tts";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { WSClient } from "../io/WSClient";
import type { ServerEventRouter } from "../io/ServerEventRouter";

const agentInfo: AgentInfo = { id: "a1", name: "alpha", state: "running", protected: false };

function makeRouter(connected = true) {
    return { emit: vi.fn(() => connected) } as unknown as ServerEventRouter;
}

interface FakeWS {
    send: ReturnType<typeof vi.fn>;
    onStreamChunk: ReturnType<typeof vi.fn>;
    onStreamEnd: ReturnType<typeof vi.fn>;
    chunkCb?: (chunk: string, from: string) => void;
    endCb?: () => void;
}

function makeWS(sendResult = true): FakeWS {
    const ws: FakeWS = {
        send: vi.fn(() => sendResult),
        onStreamChunk: vi.fn((cb: (chunk: string, from: string) => void) => {
            ws.chunkCb = cb;
        }),
        onStreamEnd: vi.fn((cb: () => void) => {
            ws.endCb = cb;
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
        await io.send("hello", agentInfo);
        expect(ws.send).toHaveBeenCalledWith("@alpha hello", "alpha");
    });

    it("toasts when the WebSocket send fails", async () => {
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS(false) as unknown as WSClient);
        await io.send("hi", null);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
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
        // it used to be echoed before the attempt and left sitting there,
        // indistinguishable from a message that actually went out
        expect(lastEvent("af-feed-push")).toBeUndefined();
    });
});

describe("IOManager streaming", () => {
    it("accumulates chunks and emits af-stream-end with the full text + TTS + feed", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);

        ws.chunkCb!("Hel", "alpha");
        ws.chunkCb!("lo", "alpha");
        expect(lastEvent("af-stream-chunk")?.detail).toEqual({ chunk: "lo", from: "alpha" });

        ws.endCb!();
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "Hello", from: "alpha" });
        expect(tts.notify).toHaveBeenCalledWith("Hello");
        expect(lastEvent("af-feed-push")?.detail.item).toMatchObject({ agentName: "alpha", label: "Hello" });
    });

    it("emits an empty af-stream-end with no TTS/feed when nothing streamed", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        ws.endCb!();
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "", from: "main-actor" });
        expect(tts.notify).not.toHaveBeenCalled();
        expect(lastEvent("af-feed-push")).toBeUndefined();
    });

    it("resets the buffer between turns", () => {
        const io = new IOManager(makeRouter());
        const ws = makeWS();
        io.setWSClient(ws as unknown as WSClient);
        ws.chunkCb!("one", "alpha");
        ws.endCb!();
        ws.chunkCb!("two", "beta");
        ws.endCb!();
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
        ws.endCb!();
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
        content: "hi",
        timestampMs: 1,
        ...over,
    });

    it("reads a user-directed reply aloud", () => {
        new IOManager(makeRouter()).receiveAgentMessage(reply());
        expect(tts.notify).toHaveBeenCalledWith("hi", "alpha");
    });

    it("ignores agent↔agent chatter", () => {
        new IOManager(makeRouter()).receiveAgentMessage(reply({ to: "beta" }));
        expect(tts.notify).not.toHaveBeenCalled();
    });

    it("reads a non-streamed reply aloud in direct_ws mode too", () => {
        // Regression: non-streamed direct_ws replies (slash commands, one-shot,
        // errors) arrive WS-only and must speak — streamed ones already do.
        const io = new IOManager(makeRouter());
        io.setWSClient(makeWS() as unknown as WSClient);
        io.receiveAgentMessage(reply());
        expect(tts.notify).toHaveBeenCalledWith("hi", "alpha");
    });
});
