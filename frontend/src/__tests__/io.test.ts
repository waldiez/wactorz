/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));
vi.mock("../io/TTSManager", () => ({ tts: { notify: vi.fn() } }));

import { IOManager } from "../io/IOManager";
import { toast } from "../ui/ToastManager";
import { tts } from "../io/TTSManager";
import type { AgentInfo, ChatMessage } from "../types/agent";
import type { WSChatClient } from "../io/WSChatClient";
import type { MQTTClient } from "../mqtt/MQTTClient";

const agentInfo: AgentInfo = { id: "a1", name: "alpha", state: "running", protected: false };

function makeMqtt(connected = true) {
    return { publish: vi.fn(() => connected) } as unknown as MQTTClient;
}

interface FakeWS {
    chatMode: "direct_ws" | "mqtt";
    send: ReturnType<typeof vi.fn>;
    onStreamChunk: ReturnType<typeof vi.fn>;
    onStreamEnd: ReturnType<typeof vi.fn>;
    chunkCb?: (chunk: string, from: string) => void;
    endCb?: () => void;
}

function makeWS(mode: "direct_ws" | "mqtt" = "mqtt", sendResult = true): FakeWS {
    const ws: FakeWS = {
        chatMode: mode,
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

describe("IOManager.send — mqtt mode", () => {
    it("publishes to io/chat with the @name prefix when an agent is selected", async () => {
        const mqtt = makeMqtt();
        await new IOManager(mqtt).send("hello", agentInfo);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({ to: "alpha", content: "@alpha hello" }),
        );
    });

    it("defaults to main-actor and no prefix when no agent is selected", async () => {
        const mqtt = makeMqtt();
        await new IOManager(mqtt).send("hi", null);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({ to: "main-actor", content: "hi" }),
        );
    });

    it("pushes the user message to the feed", async () => {
        await new IOManager(makeMqtt()).send("hello", agentInfo);
        expect(lastEvent("af-feed-push")?.detail.item).toMatchObject({ agentName: "user", label: "hello" });
    });

    it("toasts an error when the publish fails", async () => {
        await new IOManager(makeMqtt(false)).send("hi", null);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
    });
});

describe("IOManager.send — direct_ws mode", () => {
    it("sends over the WebSocket and never publishes to MQTT", async () => {
        const mqtt = makeMqtt();
        const io = new IOManager(mqtt);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as unknown as WSChatClient);
        await io.send("hello", agentInfo);
        expect(ws.send).toHaveBeenCalledWith("@alpha hello", "alpha");
        expect(mqtt.publish).not.toHaveBeenCalled();
    });

    it("toasts when the WebSocket send fails", async () => {
        const io = new IOManager(makeMqtt());
        io.setWSClient(makeWS("direct_ws", false) as unknown as WSChatClient);
        await io.send("hi", null);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
    });
});

describe("IOManager streaming", () => {
    it("accumulates chunks and emits af-stream-end with the full text + TTS + feed", () => {
        const io = new IOManager(makeMqtt());
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as unknown as WSChatClient);

        ws.chunkCb!("Hel", "alpha");
        ws.chunkCb!("lo", "alpha");
        expect(lastEvent("af-stream-chunk")?.detail).toEqual({ chunk: "lo", from: "alpha" });

        ws.endCb!();
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "Hello", from: "alpha" });
        expect(tts.notify).toHaveBeenCalledWith("Hello");
        expect(lastEvent("af-feed-push")?.detail.item).toMatchObject({ agentName: "alpha", label: "Hello" });
    });

    it("emits an empty af-stream-end with no TTS/feed when nothing streamed", () => {
        const io = new IOManager(makeMqtt());
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as unknown as WSChatClient);
        ws.endCb!();
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "", from: "main-actor" });
        expect(tts.notify).not.toHaveBeenCalled();
        expect(lastEvent("af-feed-push")).toBeUndefined();
    });

    it("resets the buffer between turns", () => {
        const io = new IOManager(makeMqtt());
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as unknown as WSChatClient);
        ws.chunkCb!("one", "alpha");
        ws.endCb!();
        ws.chunkCb!("two", "beta");
        ws.endCb!();
        expect(lastEvent("af-stream-end")?.detail).toEqual({ text: "two", from: "beta" });
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

    it("reads a user-directed reply aloud in mqtt mode", () => {
        new IOManager(makeMqtt()).receiveAgentMessage(reply());
        expect(tts.notify).toHaveBeenCalledWith("hi", "alpha");
    });

    it("ignores agent↔agent chatter", () => {
        new IOManager(makeMqtt()).receiveAgentMessage(reply({ to: "beta" }));
        expect(tts.notify).not.toHaveBeenCalled();
    });

    it("stays silent in direct_ws mode (the WS path handles replies)", () => {
        const io = new IOManager(makeMqtt());
        io.setWSClient(makeWS("direct_ws") as unknown as WSChatClient);
        io.receiveAgentMessage(reply());
        expect(tts.notify).not.toHaveBeenCalled();
    });
});
