/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { WSChatClient } from "../io/WSChatClient";

class MockWebSocket {
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    readyState: number;
    listeners: Record<string, ((e: unknown) => void)[]> = {};
    sent: string[] = [];
    url: string;

    constructor(url: string) {
        this.url = url;
        this.readyState = MockWebSocket.OPEN;
        instances.push(this);
    }

    addEventListener(event: string, cb: (e: unknown) => void) {
        if (!this.listeners[event]) {
            this.listeners[event] = [];
        }
        this.listeners[event].push(cb);
    }

    send(data: string) {
        this.sent.push(data);
    }
    close() {
        this.readyState = MockWebSocket.CLOSED;
    }

    emit(event: string, payload: unknown) {
        this.listeners[event]?.forEach(fn => fn(payload));
    }
}

const instances: MockWebSocket[] = [];
(globalThis as any).WebSocket = MockWebSocket;

function ws(): MockWebSocket {
    return instances[instances.length - 1]!;
}

beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
});

describe("WSChatClient", () => {
    it("chatMode defaults to 'mqtt'", () => {
        const c = new WSChatClient();
        expect(c.chatMode).toBe("mqtt");
    });

    it("connected is false before connect()", () => {
        const c = new WSChatClient();
        expect(c.connected).toBe(false);
    });

    it("opens a WebSocket on connect()", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        expect(ws()).toBeDefined();
        expect(ws().url).toBe("ws://localhost/ws");
    });

    it("open event handler logs connection URL", () => {
        const spy = vi.spyOn(console, "info").mockImplementation(() => {});
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        ws().emit("open", {});
        expect(spy).toHaveBeenCalledWith("[WSChat] connected →", "ws://localhost/ws");
        spy.mockRestore();
        void c;
    });

    it("connected returns true when socket is OPEN", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        expect(c.connected).toBe(true);
    });

    it("updates chatMode to 'direct_ws' on config message", () => {
        const c = new WSChatClient();
        const modeSpy = vi.fn();
        c.onMode(modeSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ type: "config", chat_mode: "direct_ws" }) });
        expect(c.chatMode).toBe("direct_ws");
        expect(modeSpy).toHaveBeenCalledWith("direct_ws");
    });

    it("sets chatMode to 'mqtt' for unknown chat_mode value", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ type: "config", chat_mode: "something-else" }) });
        expect(c.chatMode).toBe("mqtt");
    });

    it("calls onChat handler for type='chat'", () => {
        const c = new WSChatClient();
        const chatSpy = vi.fn();
        c.onChat(chatSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "chat",
                content: "Hello!",
                from: "io-agent",
                timestamp: 1_700_000_000,
            }),
        });
        expect(chatSpy).toHaveBeenCalledWith("Hello!", "io-agent", 1_700_000_000_000, "user");
    });

    it("converts ms timestamp correctly in chat", () => {
        const c = new WSChatClient();
        const chatSpy = vi.fn();
        c.onChat(chatSpy);
        c.connect("ws://localhost/ws");
        const ts = 1_700_000_000_000;
        ws().emit("message", { data: JSON.stringify({ type: "chat", content: "Hi", timestamp: ts }) });
        expect(chatSpy.mock.calls[0]![2]).toBe(ts);
    });

    it("attributes an io-gateway / absent reply to the last addressed agent", () => {
        const c = new WSChatClient();
        const chatSpy = vi.fn();
        c.onChat(chatSpy);
        c.connect("ws://localhost/ws");
        // Before any send, the gateway/absent sender falls back to the default agent.
        ws().emit("message", { data: JSON.stringify({ type: "chat", content: "Hi" }) });
        expect(chatSpy.mock.calls[0]![1]).toBe("main-actor");
        // After addressing a specific agent, the gateway reply is re-attributed to it.
        c.send("hello", "catalog");
        ws().emit("message", {
            data: JSON.stringify({ type: "chat", content: "Yo", from: "io-gateway" }),
        });
        expect(chatSpy.mock.calls[1]![1]).toBe("catalog");
    });

    it("passes through a real agent sender unchanged", () => {
        const c = new WSChatClient();
        const chatSpy = vi.fn();
        c.onChat(chatSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "chat", content: "Hi", from: "home-assistant-agent" }),
        });
        expect(chatSpy.mock.calls[0]![1]).toBe("home-assistant-agent");
    });

    it("calls onStreamChunk for type='stream_chunk'", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStreamChunk(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "stream_chunk", content: "part", from: "main" }),
        });
        expect(spy).toHaveBeenCalledWith("part", "main", expect.any(Number));
    });

    it("calls onStreamEnd for type='stream_end'", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStreamEnd(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ type: "stream_end", from: "main" }) });
        expect(spy).toHaveBeenCalledWith("main");
    });

    it("calls onStatePatch when state field is present", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        const agents = [{ agent_id: "a1", name: "alpha" }];
        ws().emit("message", { data: JSON.stringify({ state: { agents } }) });
        expect(spy).toHaveBeenCalledWith(agents, undefined, {});
    });

    it("passes total_cost_usd and total_messages from state patch", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        const agents = [{ agent_id: "a1", name: "alpha" }];
        ws().emit("message", {
            data: JSON.stringify({ state: { agents, total_cost_usd: 0.042, total_messages: 7 } }),
        });
        expect(spy).toHaveBeenCalledWith(agents, undefined, { totalCostUsd: 0.042, totalMessages: 7 });
    });

    it("calls onStatePatch with deletedId for type='delete_agent'", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "delete_agent", agent_id: "gone-id", state: { agents: [] } }),
        });
        expect(spy).toHaveBeenCalledWith([], "gone-id", {});
    });

    it("passes stats on delete_agent patch", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "delete_agent",
                agent_id: "gone-id",
                state: { agents: [], total_cost_usd: 1.5, total_messages: 42 },
            }),
        });
        expect(spy).toHaveBeenCalledWith([], "gone-id", { totalCostUsd: 1.5, totalMessages: 42 });
    });

    it("state patch without agents array passes empty array", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ state: {} }) });
        expect(spy).toHaveBeenCalledWith([], undefined, {});
    });

    it("send() sends JSON to the WebSocket and returns true", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const ok = c.send("hello", "main-actor");
        expect(ok).toBe(true);
        expect(JSON.parse(ws().sent[0]!)).toEqual({
            type: "chat",
            content: "hello",
            agent_name: "main-actor",
        });
    });

    it("send() defaults agentName to 'main-actor'", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        c.send("hi");
        expect(JSON.parse(ws().sent[0]!).agent_name).toBe("main-actor");
    });

    it("send() returns false when not connected", () => {
        const c = new WSChatClient();
        expect(c.send("hi")).toBe(false);
    });

    it("sendRaw() sends arbitrary JSON and returns true", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const ok = c.sendRaw({ type: "ping" });
        expect(ok).toBe(true);
        expect(JSON.parse(ws().sent[0]!)).toEqual({ type: "ping" });
    });

    it("sendRaw() returns false when not connected", () => {
        const c = new WSChatClient();
        expect(c.sendRaw({ type: "ping" })).toBe(false);
    });

    it("disconnect() closes the socket", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        c.disconnect();
        expect(c.connected).toBe(false);
    });

    it("disconnect() cancels pending reconnect timer", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        // Trigger a close to schedule reconnect
        ws().readyState = MockWebSocket.CLOSED;
        ws().emit("close", {});
        // Now disconnect — should cancel the timer
        c.disconnect();
        vi.runAllTimers();
        // Only 1 WebSocket instance — no reconnect happened
        expect(instances.length).toBe(1);
    });

    it("reconnects after close when not intentionally disconnected", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        ws().readyState = MockWebSocket.CLOSED;
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        expect(instances.length).toBe(2);
    });

    it("does not double-schedule reconnect on repeated close events", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        ws().emit("close", {});
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        expect(instances.length).toBe(2);
        c.disconnect();
    });

    it("reconnect delay doubles on each attempt (exponential backoff)", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        // First close → 1000ms delay
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        expect(instances.length).toBe(2);
        // Second close → 2000ms delay
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        expect(instances.length).toBe(2); // not yet
        vi.advanceTimersByTime(1001);
        expect(instances.length).toBe(3);
        c.disconnect();
    });

    it("reconnect delay resets to 1000ms after successful open", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        // Trigger two failures to bump delay to 2000ms
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        ws().emit("close", {});
        vi.advanceTimersByTime(2001);
        // Now fire "open" to reset delay
        ws().emit("open", {});
        ws().emit("close", {});
        vi.advanceTimersByTime(1001);
        // Should reconnect at 1000ms again (total 4 instances)
        expect(instances.length).toBe(4);
        c.disconnect();
    });

    it("handles WebSocket error events without throwing", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        expect(() => ws().emit("error", new Error("refused"))).not.toThrow();
    });

    it("ignores malformed JSON messages", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onChat(spy);
        c.connect("ws://localhost/ws");
        expect(() => ws().emit("message", { data: "not json" })).not.toThrow();
        expect(spy).not.toHaveBeenCalled();
    });

    it("handles WebSocket constructor throwing and schedules reconnect", () => {
        const original = (globalThis as any).WebSocket;
        (globalThis as any).WebSocket = class {
            constructor() {
                throw new Error("no ws");
            }
        };
        const c = new WSChatClient();
        expect(() => c.connect("ws://bad")).not.toThrow();
        vi.advanceTimersByTime(1001);
        (globalThis as any).WebSocket = original;
        c.disconnect();
    });

    it("onLogFeed() registers callback", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        expect(() => c.onLogFeed(spy)).not.toThrow();
    });

    it("reset message calls onStatePatch and clears log_feed via onLogFeed", () => {
        // A scoped (non-"all") reset applies the state patch; "all" early-returns
        // through af-wipe-all and is covered separately below.
        const c = new WSChatClient();
        const patchSpy = vi.fn();
        const feedSpy = vi.fn();
        c.onStatePatch(patchSpy);
        c.onLogFeed(feedSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "reset",
                scope: "state",
                state: {
                    agents: [{ agent_id: "a1", name: "alpha" }],
                    log_feed: [{ ts: 1, msg: "hi" }],
                },
            }),
        });
        expect(patchSpy).toHaveBeenCalledWith([{ agent_id: "a1", name: "alpha" }], undefined, {});
        expect(feedSpy).toHaveBeenCalledWith([{ ts: 1, msg: "hi" }]);
    });

    it("reset message with scope='chat' dispatches af-reset-chat event", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const eventSpy = vi.fn();
        document.addEventListener("af-reset-chat", eventSpy, { once: true });
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", scope: "chat", state: { agents: [] } }),
        });
        expect(eventSpy).toHaveBeenCalled();
    });

    it("reset message with scope='all' dispatches af-wipe-all event", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const eventSpy = vi.fn();
        document.addEventListener("af-wipe-all", eventSpy, { once: true });
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", scope: "all", state: { agents: [] } }),
        });
        expect(eventSpy).toHaveBeenCalled();
    });

    it("reset message with scope='logs' does not dispatch af-reset-chat event", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const eventSpy = vi.fn();
        document.addEventListener("af-reset-chat", eventSpy, { once: true });
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", scope: "logs", state: { agents: [] } }),
        });
        expect(eventSpy).not.toHaveBeenCalled();
        document.removeEventListener("af-reset-chat", eventSpy);
    });

    it("reset message with total_cost_usd and total_messages passes stats", () => {
        const c = new WSChatClient();
        const patchSpy = vi.fn();
        c.onStatePatch(patchSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "reset",
                scope: "state",
                state: { agents: [], total_cost_usd: 2.5, total_messages: 10 },
            }),
        });
        expect(patchSpy).toHaveBeenCalledWith([], undefined, { totalCostUsd: 2.5, totalMessages: 10 });
    });

    it("reset message with no log_feed does not call onLogFeed", () => {
        const c = new WSChatClient();
        const feedSpy = vi.fn();
        c.onLogFeed(feedSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", scope: "state", state: { agents: [] } }),
        });
        expect(feedSpy).not.toHaveBeenCalled();
    });

    it("chat message without onChat registered does not throw", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        // _onChat is null → _onChat?.() skips gracefully
        expect(() =>
            ws().emit("message", { data: JSON.stringify({ type: "chat", content: "hi" }) }),
        ).not.toThrow();
    });

    it("stream_chunk without onStreamChunk registered does not throw", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        expect(() =>
            ws().emit("message", { data: JSON.stringify({ type: "stream_chunk", content: "part" }) }),
        ).not.toThrow();
    });

    it("state patch with log_feed but no onLogFeed registered does not throw", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        expect(() =>
            ws().emit("message", {
                data: JSON.stringify({ state: { log_feed: [{ ts: 1, msg: "hi" }] } }),
            }),
        ).not.toThrow();
    });

    it("reset message without scope field uses empty string for scope (covers ?? '')", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        const eventSpy = vi.fn();
        document.addEventListener("af-reset-chat", eventSpy, { once: true });
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", state: { agents: [] } }), // no scope field
        });
        expect(eventSpy).not.toHaveBeenCalled(); // scope="" doesn't match "chat" or "all"
        document.removeEventListener("af-reset-chat", eventSpy);
    });

    it("reset message without agents in state uses empty array (covers ?? [])", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "reset", scope: "state", state: {} }), // no agents field
        });
        expect(spy).toHaveBeenCalledWith([], undefined, {});
    });

    it("delete_agent with log_feed in state calls onLogFeed (covers line 228 truthy branch)", () => {
        const c = new WSChatClient();
        const feedSpy = vi.fn();
        c.onLogFeed(feedSpy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "delete_agent",
                agent_id: "gone",
                state: { agents: [], log_feed: [{ ts: 1, msg: "bye" }] },
            }),
        });
        expect(feedSpy).toHaveBeenCalledWith([{ ts: 1, msg: "bye" }]);
    });

    it("delete_agent without agent_id uses empty string (covers ?? '')", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStatePatch(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ type: "delete_agent", state: { agents: [] } }), // no agent_id
        });
        expect(spy).toHaveBeenCalledWith([], "", {});
    });

    it("close event after intentional disconnect does not schedule reconnect", () => {
        const c = new WSChatClient();
        c.connect("ws://localhost/ws");
        c.disconnect(); // _closed = true
        ws().emit("close", {}); // !_closed is false → no reconnect
        vi.runAllTimers();
        expect(instances.length).toBe(1); // no new connection
    });

    it("chat message with no content uses empty string", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onChat(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ type: "chat", from: "agent" }) });
        expect(spy.mock.calls[0]![0]).toBe("");
    });

    it("forwards a voice user's chat target", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onChat(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({
                type: "chat",
                from: "user",
                to: "reachy-mini",
                content: "turn on the light",
            }),
        });
        expect(spy.mock.calls[0]![3]).toBe("reachy-mini");
    });
    it("stream_chunk with no content uses empty string", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onStreamChunk(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", { data: JSON.stringify({ type: "stream_chunk", from: "agent" }) });
        expect(spy.mock.calls[0]![0]).toBe("");
    });

    it("state patch with empty log_feed array does not call onLogFeed", () => {
        const c = new WSChatClient();
        const spy = vi.fn();
        c.onLogFeed(spy);
        c.connect("ws://localhost/ws");
        ws().emit("message", {
            data: JSON.stringify({ state: { agents: [], log_feed: [] } }),
        });
        expect(spy).not.toHaveBeenCalled();
    });
});
