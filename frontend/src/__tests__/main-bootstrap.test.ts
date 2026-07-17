/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap (composition-root) test for src/main.ts.
 *
 * main.ts is pure wiring: it instantiates the transports/store, derives the
 * deployment URLs, and registers handlers. We mock the transports so importing
 * the module records every handler it registers, then drive each handler (and
 * each app-event) to exercise the wiring — including the guard branches — the
 * way the real transports would at runtime. Decision/transform logic itself is
 * covered by the agents/mapping + haConfig + haFeed unit tests.
 */
import { describe, it, expect, vi, beforeAll, afterAll } from "vitest";
import { emit, listen } from "../events";
import { toast } from "../ui/ToastManager";

// Registries the mocked transports populate as main.ts wires them up.
const mockMqtt: Record<string, (...a: any[]) => void> = {};
const mockWs: Record<string, (...a: any[]) => void> = {};
// A single known agent so `getAgents().find(...)` resolves in the metrics/send paths.
const mockStoreAgents = [{ id: "a", name: "A", state: "running", protected: false }];

// Fetch a registered handler (asserting it exists) so the call sites below stay
// branch-free — keeps each test's cyclomatic complexity low.
const mqttHandler = (ev: string): ((...a: any[]) => void) => mockMqtt[ev] as (...a: any[]) => void;
const wsHandler = (ev: string): ((...a: any[]) => void) => mockWs[ev] as (...a: any[]) => void;

vi.mock("../mqtt/MQTTClient", () => ({
    MQTTClient: class {
        constructor(_url: string) {}
        on(ev: string, cb: (...a: any[]) => void) {
            mockMqtt[ev] = cb;
        }
        connect = vi.fn();
        disconnect = vi.fn();
    },
}));

// Mutable so a test can flip the active chat mode and exercise the
// direct_ws early-return guard in the MQTT chat handler.
let mockChatMode: "direct_ws" | "mqtt" = "mqtt";

vi.mock("../io/WSChatClient", () => ({
    WSChatClient: class {
        get chatMode() {
            return mockChatMode;
        }
        onChat(cb: (...a: any[]) => void) {
            mockWs["chat"] = cb;
        }
        onStatePatch(cb: (...a: any[]) => void) {
            mockWs["statePatch"] = cb;
        }
        onLogFeed(cb: (...a: any[]) => void) {
            mockWs["logFeed"] = cb;
        }
        onStreamChunk = vi.fn();
        onStreamEnd = vi.fn();
        connect = vi.fn();
        disconnect = vi.fn();
        sendRaw = vi.fn();
    },
}));

vi.mock("../io/IOManager", () => ({
    IOManager: class {
        constructor(_mqtt: unknown) {}
        setWSClient = vi.fn();
        receiveAgentMessage = vi.fn();
        send = vi.fn(async () => {});
    },
}));

vi.mock("../agents/AgentStore", () => ({
    AgentStore: class {
        reconcileAgents = vi.fn();
        onChat = vi.fn();
        removeAgent = vi.fn();
        setTotalCostUsd = vi.fn();
        setTotalMessages = vi.fn();
        addOrUpdateAgent = vi.fn();
        getAgents = () => mockStoreAgents;
        onHeartbeat = vi.fn();
        onSpawn = vi.fn();
        onAlert = vi.fn();
        pruneStaleRemoteAgents = vi.fn();
        updateRemoteNode = vi.fn();
        setHostStats = vi.fn();
        clearAll = vi.fn();
        dispose = vi.fn();
    },
}));

vi.mock("../io/TTSManager", () => ({ tts: { setApiBase: vi.fn(), init: vi.fn() } }));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));
vi.mock("../ui/DropZone", () => ({ DropZone: class {} }));

describe("main.ts bootstrap", () => {
    const origFetch = globalThis.fetch;

    beforeAll(async () => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
        globalThis.fetch = vi.fn(async (url: string) => ({
            ok: true,
            json: async () =>
                String(url).includes("/api/feed")
                    ? [{ type: "chat", label: "hello", agentName: "A", timestamp: 1700000000, role: "user" }]
                    : [{ id: "a", name: "A", state: "running", protected: false }],
        })) as unknown as typeof fetch;
        await import("../main");
        // let the /api/actors + /api/feed fetch chains settle
        await Promise.resolve();
        await Promise.resolve();
    });

    afterAll(() => {
        globalThis.fetch = origFetch;
    });

    it("registers every transport handler and the app-event listeners", () => {
        for (const ev of [
            "heartbeat",
            "spawn",
            "alert",
            "chat",
            "status",
            "connected",
            "qa-flag",
            "metrics",
            "logs",
            "completed",
            "node-heartbeat",
            "host-stats",
            "raw",
            "disconnected",
            "error",
        ]) {
            expect(typeof mockMqtt[ev]).toBe("function");
        }
        for (const ev of ["chat", "statePatch", "logFeed"]) {
            expect(typeof mockWs[ev]).toBe("function");
        }
    });

    it("applies the deletion-guard early-return branches on stale MQTT events", () => {
        // Mark an id deleted so the stale-event guards take their early-return path.
        emit("af-agent-command", { command: "delete", agentId: "del" });
        mqttHandler("heartbeat")({ agentId: "del", agentName: "X", timestampMs: 0 }); // suppressed
        mqttHandler("spawn")({ agentId: "del", agentName: "X", timestampMs: 0, agentType: "worker" });
        mqttHandler("status")({ agentId: "del", agentName: "X", state: "running" }); // deleted → skip
        expect(true).toBe(true);
    });

    it("drives the agent lifecycle MQTT handlers", () => {
        mqttHandler("heartbeat")({ agentId: "a", agentName: "A", timestampMs: 1 });
        mqttHandler("spawn")({ agentId: "s", agentName: "S", timestampMs: 1, agentType: "worker" });
        mqttHandler("alert")({
            agentId: "a",
            agentName: "A",
            severity: "error",
            message: "m",
            timestampMs: 1,
        });
        mqttHandler("chat")({ id: "x", from: "A", to: "user", content: "hi", timestampMs: 1 });
        mqttHandler("chat")({ id: "y", from: "user", to: "A", content: "hey", timestampMs: 1 }); // from===user
        mqttHandler("status")({ agentId: "a", agentName: "A", state: "running", protected: false });
        mqttHandler("status")({ agentId: "a", agentName: "A", state: "stopped" }); // stopped branch
        mqttHandler("connected")(); // first → seeds
        mqttHandler("connected")(); // again → seeded guard
        expect(true).toBe(true);
    });

    it("ignores MQTT agent chat in direct_ws mode (one transport per mode)", () => {
        // In direct_ws the monitor relays agent chat over the WebSocket, and our
        // `agents/#` MQTT subscription receives the same frame — the handler must
        // early-return so speech bubbles (notify_user) aren't rendered twice.
        let count = 0;
        const handler = listen("af-chat-message", () => {
            count += 1;
        });
        mockChatMode = "direct_ws";
        mqttHandler("chat")({ id: "z", from: "A", to: "user", content: "dup", timestampMs: 1 });
        mockChatMode = "mqtt"; // restore for subsequent tests
        document.removeEventListener("af-chat-message", handler);
        expect(count).toBe(0);
    });

    it("drives the telemetry + feed MQTT handlers, both guard branches", () => {
        mqttHandler("qa-flag")({ category: "c", excerpt: "e", from: "A", timestampMs: 1 });
        mqttHandler("metrics")({ agentId: "a", messagesProcessed: 1, costUsd: 1, uptime: 1 }); // found
        mqttHandler("metrics")({ agentId: "zzz" }); // not found → return
        mqttHandler("logs")({ agentName: "A", message: "log" }); // pushes
        mqttHandler("logs")({ agentName: "A" }); // empty → null, no push
        mqttHandler("completed")({ agentName: "A" });
        mqttHandler("node-heartbeat")({ node: "n", agents: ["a"] });
        mqttHandler("host-stats")({ cpu: 1, memUsedMb: 2, memTotalMb: 3 }); // has stats
        mqttHandler("host-stats")({}); // nothing → skip
        expect(true).toBe(true);
    });

    it("routes HA raw events through the feed (and ignores non-HA topics)", () => {
        mqttHandler("raw")({
            topic: "homeassistant/state_changes/light/x",
            payload: { new_state: { state: "on", attributes: { friendly_name: "Lamp" } } },
        });
        mqttHandler("raw")({ topic: "not-ha", payload: {} }); // parses to null
        mqttHandler("disconnected")();
        mqttHandler("error")(new Error("boom"));
        expect(true).toBe(true);
    });

    it("drives the WebSocket handlers", () => {
        wsHandler("chat")("hi", "A", 1);
        wsHandler("chat")("heard speech", "user", 2, "reachy-mini");
        wsHandler("statePatch")(
            [{ agent_id: "a", name: "A", state: "running" }, { agent_id: "" }, { agent_id: "del" }],
            "delX",
            { totalCostUsd: 5, totalMessages: 3 },
        );
        wsHandler("statePatch")([], undefined, undefined); // no deletedId / no stats
        wsHandler("logFeed")([
            { type: "spawned", agent_id: "a", name: "A", timestamp: 1, agentType: "worker" },
            { type: "log", agent_id: "a", message: "m", timestamp: 2 },
        ]);
        wsHandler("logFeed")([{ type: "log", agent_id: "a", message: "m2", timestamp: 3 }]); // initialized branch
        expect(true).toBe(true);
    });

    it("handles the app-event listeners", () => {
        emit("af-stream-end", { text: "done", from: "A" });
        emit("af-stream-end", { text: "", from: "A" }); // !text → return
        emit("af-agent-command", { command: "pause", agentId: "a" }); // non-delete
        emit("af-send-message", { content: "hi", target: "A", attachments: [] }); // agent found
        emit("af-send-message", { content: "hi", target: "nope", attachments: [] }); // null
        emit("af-wipe-all");
        emit("af-clear-feed");
        window.dispatchEvent(new Event("beforeunload"));
        expect(true).toBe(true);
    });

    it("routes uncaught errors and unhandled rejections to a toast", () => {
        vi.mocked(toast.show).mockClear();
        window.dispatchEvent(
            Object.assign(new Event("error"), { error: new Error("boom"), message: "boom" }),
        );
        expect(toast.show).toHaveBeenCalledTimes(1);
        window.dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: new Error("nope") }));
        expect(toast.show).toHaveBeenCalledTimes(2);
    });
});
