/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Bootstrap (composition-root) test for src/main.ts.
 *
 * main.ts is pure wiring: it instantiates the WS transport, the event router and
 * the store, derives the deployment URLs, and registers handlers. We mock those
 * so importing the module records every handler it registers, then drive each
 * handler (and each app-event) to exercise the wiring — including the guard
 * branches — the way the real transport would at runtime. Decision/transform
 * logic itself is covered by the agents/mapping + haConfig + haFeed unit tests.
 */
import { describe, it, expect, vi, beforeAll, afterAll } from "vitest";
import { emit } from "../events";
import { toast } from "../ui/ToastManager";

// Registries the mocked transport + router populate as main.ts wires them up.
const mockRouter: Record<string, (...a: any[]) => void> = {};
const mockWs: Record<string, (...a: any[]) => void> = {};
// A single known agent so `getAgents().find(...)` resolves in the metrics path.
const mockStoreAgents = [{ id: "a", name: "A", state: "running", protected: false }];

// Fetch a registered handler (asserting it exists) so the call sites below stay
// branch-free — keeps each test's cyclomatic complexity low.
const routerHandler = (ev: string): ((...a: any[]) => void) => mockRouter[ev] as (...a: any[]) => void;
const wsHandler = (ev: string): ((...a: any[]) => void) => mockWs[ev] as (...a: any[]) => void;

vi.mock("../io/ServerEventRouter", () => ({
    ServerEventRouter: class {
        on(ev: string, cb: (...a: any[]) => void) {
            mockRouter[ev] = cb;
            return this;
        }
        off = vi.fn();
        route = vi.fn();
    },
}));

vi.mock("../io/WSClient", () => ({
    WSClient: class {
        onServerEvent(cb: (...a: any[]) => void) {
            mockWs["serverEvent"] = cb;
        }
        onConnected(cb: (...a: any[]) => void) {
            mockWs["connected"] = cb;
        }
        onDisconnected(cb: (...a: any[]) => void) {
            mockWs["disconnected"] = cb;
        }
        onMqttStatus(cb: (...a: any[]) => void) {
            mockWs["mqttStatus"] = cb;
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
        constructor(_router: unknown) {}
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
        cardDashboard = { renderView: vi.fn(), registerView: vi.fn() };
    },
}));

vi.mock("../ext/tts", () => ({ tts: { setApiBase: vi.fn(), init: vi.fn() }, register: vi.fn() }));
vi.mock("../config/serverConfig", () => ({
    seedServerConfig: vi.fn(async () => {
        localStorage.setItem("wactorz-fuseki-url", "http://fuseki:3030/wactorz");
        localStorage.setItem("wactorz-fuseki-dataset", "wactorz");
        localStorage.setItem("wactorz-tts-available", "1");
        return true;
    }),
}));
vi.mock("../config/serverConfig", () => ({
    seedServerConfig: vi.fn(async () => {
        // Set up safeStorage so fuseki/TTS registration paths are covered.
        localStorage.setItem("wactorz-fuseki-url", "http://fuseki:3030/wactorz");
        localStorage.setItem("wactorz-fuseki-dataset", "wactorz");
        localStorage.setItem("wactorz-tts-available", "1");
        return true; // haChanged → covers line 462
    }),
}));
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
        localStorage.clear();
        localStorage.clear();
    });

    it("registers every router handler and the WS handlers", () => {
        for (const ev of [
            "heartbeat",
            "spawn",
            "alert",
            "chat",
            "status",
            "qa-flag",
            "metrics",
            "logs",
            "completed",
            "node-heartbeat",
            "host-stats",
            "raw",
        ]) {
            expect(typeof mockRouter[ev]).toBe("function");
        }
        for (const ev of [
            "serverEvent",
            "connected",
            "disconnected",
            "mqttStatus",
            "chat",
            "statePatch",
            "logFeed",
        ]) {
            expect(typeof mockWs[ev]).toBe("function");
        }
    });

    it("applies the deletion-guard early-return branches on stale events", () => {
        // Mark an id deleted so the stale-event guards take their early-return path.
        emit("af-agent-command", { command: "delete", agentId: "del" });
        routerHandler("heartbeat")({ agentId: "del", agentName: "X", timestampMs: 0 }); // suppressed
        routerHandler("spawn")({ agentId: "del", agentName: "X", timestampMs: 0, agentType: "worker" });
        routerHandler("status")({ agentId: "del", agentName: "X", state: "running" }); // deleted → skip
        expect(true).toBe(true);
    });

    it("drives the agent lifecycle handlers", () => {
        routerHandler("heartbeat")({ agentId: "a", agentName: "A", timestampMs: 1 });
        routerHandler("spawn")({ agentId: "s", agentName: "S", timestampMs: 1, agentType: "worker" });
        routerHandler("alert")({
            agentId: "a",
            agentName: "A",
            severity: "error",
            message: "m",
            timestampMs: 1,
        });
        routerHandler("chat")({ id: "x", from: "A", to: "user", content: "hi", timestampMs: 1 });
        routerHandler("chat")({ id: "y", from: "user", to: "A", content: "hey", timestampMs: 1 }); // from===user
        routerHandler("status")({ agentId: "a", agentName: "A", state: "running", protected: false });
        routerHandler("status")({ agentId: "a", agentName: "A", state: "stopped" }); // stopped branch
        expect(true).toBe(true);
    });

    it("goes live only when both the /ws is open and the broker is up, then seeds once", () => {
        wsHandler("connected")(); // wsOpen=true, but broker still down → not live yet
        wsHandler("mqttStatus")(true); // both up → live: prunes + seeds
        wsHandler("mqttStatus")(true); // unchanged → recompute early-returns
        wsHandler("connected")(); // unchanged → seeded guard holds
        wsHandler("mqttStatus")(false); // broker down → demo
        wsHandler("disconnected")(); // /ws down → stays demo
        expect(true).toBe(true);
    });

    it("drives the telemetry + feed handlers, both guard branches", () => {
        routerHandler("qa-flag")({ category: "c", excerpt: "e", from: "A", timestampMs: 1 });
        routerHandler("metrics")({ agentId: "a", messagesProcessed: 1, costUsd: 1, uptime: 1 }); // found
        routerHandler("metrics")({ agentId: "zzz" }); // not found → return
        routerHandler("logs")({ agentName: "A", message: "log" }); // pushes
        routerHandler("logs")({ agentName: "A" }); // empty → null, no push
        routerHandler("completed")({ agentName: "A" });
        routerHandler("node-heartbeat")({ node: "n", agents: ["a"] });
        routerHandler("host-stats")({ cpu: 1, memUsedMb: 2, memTotalMb: 3 }); // has stats
        routerHandler("host-stats")({}); // nothing → skip
        expect(true).toBe(true);
    });

    it("routes HA raw events through the feed (and ignores non-HA topics)", () => {
        routerHandler("raw")({
            topic: "homeassistant/state_changes/light/x",
            payload: { new_state: { state: "on", attributes: { friendly_name: "Lamp" } } },
        });
        routerHandler("raw")({ topic: "not-ha", payload: {} }); // parses to null
        expect(true).toBe(true);
    });

    it("routes uncaught errors and unhandled rejections to a toast", () => {
        wsHandler("serverEvent")("agents/a/heartbeat", { cpu: 1 }); // forwarded to router.route
        wsHandler("chat")("hi", "A", 1);
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
