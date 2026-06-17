/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { IOManager } from "../io/IOManager";
import type { AgentInfo, ChatMessage } from "../types/agent";

function makeMqtt(connected = true) {
    return {
        publish: vi.fn(() => connected),
    } as unknown as import("../mqtt/MQTTClient").MQTTClient;
}

function makeChatPanel() {
    return {
        ensureOpen: vi.fn(),
        appendMessage: vi.fn(),
        showTyping: vi.fn(),
        hideTyping: vi.fn(),
        streamChunk: vi.fn(),
        finalizeStream: vi.fn(),
        lastStreamedText: "",
    } as unknown as import("../ui/ChatPanel").ChatPanel;
}

function makeWS(mode: "direct_ws" | "mqtt" = "mqtt", sendResult = true) {
    return {
        chatMode: mode,
        onStreamChunk: vi.fn(),
        onStreamEnd: vi.fn(),
        send: vi.fn(() => sendResult),
    } as unknown as import("../io/WSChatClient").WSChatClient;
}

const agentInfo: AgentInfo = {
    id: "20260325T151725.0000Z-alpha",
    name: "alpha",
    state: "running",
    protected: false,
};

beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    // Suppress document events dispatched by IOManager
    vi.spyOn(document, "dispatchEvent").mockImplementation(() => true);
});

afterEach(() => {
    vi.useRealTimers();
});

describe("IOManager.send — MQTT mode", () => {
    it("publishes to io/chat with @name prefix when agent selected", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hello", agentInfo);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({
                content: "@alpha hello",
            }),
        );
    });

    it("does not prepend @name when text already starts with @", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("@other hi", agentInfo);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({
                content: "@other hi",
            }),
        );
    });

    it("publishes raw content when no agent selected", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("broadcast", null);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({
                content: "broadcast",
            }),
        );
    });

    it("appends message to panel immediately", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hi", agentInfo);
        expect(panel.appendMessage).toHaveBeenCalledWith(
            expect.objectContaining({ from: "user", content: "hi" }),
        );
    });

    it("opens the panel before appending the message", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hi", agentInfo);
        expect(panel.ensureOpen).toHaveBeenCalledBefore(panel.appendMessage as ReturnType<typeof vi.fn>);
    });

    it("shows typing indicator for the selected agent", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hello", agentInfo);
        expect(panel.showTyping).toHaveBeenCalledWith("alpha", "alpha");
    });

    it("shows error message after delay when MQTT not connected", async () => {
        const mqtt = makeMqtt(false);
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hello", null);
        vi.advanceTimersByTime(900);
        expect(panel.hideTyping).toHaveBeenCalled();
        const errorMsg = (panel.appendMessage as ReturnType<typeof vi.fn>).mock.calls.find(
            (c: unknown[]) => (c[0] as ChatMessage).from === "system",
        );
        expect(errorMsg).toBeDefined();
    });

    it("publishes to io/chat with 'to' set to main-actor when no agent", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        await io.send("hi", null);
        expect(mqtt.publish).toHaveBeenCalledWith(
            "io/chat",
            expect.objectContaining({
                to: "main-actor",
            }),
        );
    });
});

describe("IOManager.send — direct_ws mode", () => {
    it("sends via WebSocket and does not fall back to MQTT", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws", true);
        io.setWSClient(ws as any);
        await io.send("hello", agentInfo);
        expect(ws.send).toHaveBeenCalled();
        expect(mqtt.publish).not.toHaveBeenCalled();
    });

    it("shows error after delay when WS send fails", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws", false);
        io.setWSClient(ws as any);
        await io.send("hello", agentInfo);
        vi.advanceTimersByTime(400);
        expect(panel.hideTyping).toHaveBeenCalled();
        const errorMsg = (panel.appendMessage as ReturnType<typeof vi.fn>).mock.calls.find(
            (c: unknown[]) => (c[0] as ChatMessage).from === "system",
        );
        expect(errorMsg).toBeDefined();
    });

    it("setWSClient registers onStreamChunk handler", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as any);
        expect(ws.onStreamChunk).toHaveBeenCalledOnce();
    });

    it("setWSClient registers onStreamEnd handler", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as any);
        expect(ws.onStreamEnd).toHaveBeenCalledOnce();
    });

    it("stream chunk is forwarded to panel.streamChunk", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as any);
        // Invoke the registered chunk handler
        const chunkHandler = (ws.onStreamChunk as ReturnType<typeof vi.fn>).mock.calls[0]![0] as (
            chunk: string,
            from: string,
        ) => void;
        chunkHandler("hello", "main-actor");
        expect(panel.streamChunk).toHaveBeenCalledWith("hello", "main-actor");
    });

    it("stream end calls finalizeStream and hideTyping", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as any);
        const endHandler = (ws.onStreamEnd as ReturnType<typeof vi.fn>).mock.calls[0]![0] as () => void;
        endHandler();
        expect(panel.finalizeStream).toHaveBeenCalledOnce();
        expect(panel.hideTyping).toHaveBeenCalled();
    });
});

describe("IOManager.receiveAgentMessage", () => {
    function msg(overrides: Partial<ChatMessage> = {}): ChatMessage {
        return {
            id: "m1",
            from: "alpha",
            to: "user",
            content: "hi",
            timestampMs: Date.now(),
            ...overrides,
        };
    }

    it("appends message when to='user'", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        io.receiveAgentMessage(msg({ to: "user" }));
        expect(panel.appendMessage).toHaveBeenCalledOnce();
    });

    it("appends message when to is empty string", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        io.receiveAgentMessage(msg({ to: "" }));
        expect(panel.appendMessage).toHaveBeenCalledOnce();
    });

    it("ignores agent-to-agent messages (to !== 'user')", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        io.receiveAgentMessage(msg({ to: "other-agent" }));
        expect(panel.appendMessage).not.toHaveBeenCalled();
    });

    it("clears typing indicator for the sender", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        io.receiveAgentMessage(msg({ from: "main-actor", to: "user" }));
        expect(panel.hideTyping).toHaveBeenCalledWith("main-actor");
    });

    it("suppresses message when direct_ws mode is active (double-delivery guard)", () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        const ws = makeWS("direct_ws");
        io.setWSClient(ws as any);
        io.receiveAgentMessage(msg({ to: "user" }));
        expect(panel.appendMessage).not.toHaveBeenCalled();
    });

    it("clears the last typing key if different from sender", async () => {
        const mqtt = makeMqtt();
        const panel = makeChatPanel();
        const io = new IOManager(mqtt, panel);
        // The last typing key gets set when we send
        await io.send("hi", agentInfo); // sets _lastTypingKey = "alpha"
        io.receiveAgentMessage(msg({ from: "io-agent", to: "user" }));
        // Should clear both "io-agent" and "alpha"
        const calls = (panel.hideTyping as ReturnType<typeof vi.fn>).mock.calls.map((c: unknown[]) => c[0]);
        expect(calls).toContain("alpha");
        expect(calls).toContain("io-agent");
    });
});
