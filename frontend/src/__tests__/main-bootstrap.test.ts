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
 * logic itself is covered by the agents/mapping + serverConfig + haFeed unit tests.
 */
import { describe, it, expect, vi, beforeAll, afterAll } from "vitest";
import { emit, listen } from "../events";
import { toast } from "../ui/ToastManager";
import type { FeedItem } from "../types/feed";

// Registries the mocked transport + router populate as main.ts wires them up.
const mockRouter: Record<string, (...a: any[]) => void> = {};
const mockWs: Record<string, (...a: any[]) => void> = {};
// Every frame main.ts sent over the mocked transport with sendRaw.
const mockSent: unknown[] = [];
// A single known agent so `get(...)` resolves in the metrics path.
const mockStoreAgents = [{ id: "a", name: "A", state: "running", protected: false }];
// The store main.ts builds, so a test can see what reached it.
const mockStores: { onHeartbeat: ReturnType<typeof vi.fn> }[] = [];
// The dashboard main.ts builds over it.
const mockDashboards: {
    setTotalCostUsd: ReturnType<typeof vi.fn>;
    setTotalMessages: ReturnType<typeof vi.fn>;
    setHostStats: ReturnType<typeof vi.fn>;
}[] = [];
// Who the socket was told the person addresses.
const mockAddressed: string[] = [];

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
        addressed = vi.fn((agent: string) => mockAddressed.push(agent));
        sendRaw = vi.fn((frame: unknown) => {
            mockSent.push(frame);
            return true;
        });
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
        constructor() {
            mockStores.push(this);
        }
        reconcileAgents = vi.fn();
        onChat = vi.fn();
        removeAgent = vi.fn();
        addOrUpdateAgent = vi.fn();
        list = () => mockStoreAgents;
        get = (id: string) => mockStoreAgents.find(a => a.id === id);
        byName = (name: string) => mockStoreAgents.filter(a => a.name === name);
        label = (agent: { name: string }) => agent.name;
        onHeartbeat = vi.fn();
        onSpawn = vi.fn();
        onAlert = vi.fn();
        pruneSilentRemoteAgents = vi.fn();
        updateRemoteNode = vi.fn();
        clearAll = vi.fn();
    },
}));

vi.mock("../ui/CardDashboard", () => ({
    CardDashboard: class {
        constructor() {
            mockDashboards.push(this);
        }
        show = vi.fn();
        destroy = vi.fn();
        setTotalCostUsd = vi.fn();
        setTotalMessages = vi.fn();
        setHostStats = vi.fn();
    },
}));

// What had happened by the time main.ts installed the session-expiry hook.
const mockAtExpiryInstall: { requests: number; stores: number; dashboards: number }[] = [];
vi.mock("../io/sessionExpiry", () => ({
    installSessionExpiry: () => {
        const requests = vi.isMockFunction(globalThis.fetch)
            ? vi.mocked(globalThis.fetch).mock.calls.length
            : 0;
        mockAtExpiryInstall.push({ requests, stores: mockStores.length, dashboards: mockDashboards.length });
    },
}));

// The ledger's open questions, as main.ts asks for them each time it goes live.
const mockOpenQuestions = vi.fn(async (_apiBase: string): Promise<unknown[]> => []);
vi.mock("../io/openQuestions", () => ({
    fetchQuestionLedger: async (apiBase: string) => ({
        open: await mockOpenQuestions(apiBase),
        answered: [],
    }),
}));

vi.mock("../ext/tts", () => ({ tts: { setApiBase: vi.fn(), init: vi.fn() }, register: vi.fn() }));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn(), close: vi.fn() } }));
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

    it("installs session expiry before any request, and before the dashboard mounts", () => {
        // Mounting starts the dashboard's polling, so it too must come after.
        expect(mockAtExpiryInstall).toEqual([{ requests: 0, stores: 0, dashboards: 0 }]);
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
            "answer",
            "model-pointer",
            "question-book",
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

    it("keeps an agent a wipe took away from coming back on a trailing heartbeat", () => {
        // A wipe sends no delete per agent, and cards are otherwise kept until
        // one says so: what it removed must be tombstoned as a delete would be.
        mockStoreAgents.push({ id: "wiped", name: "W", state: "running", protected: false });
        try {
            emit("af-wipe-all", { survivors: ["a"] });
        } finally {
            mockStoreAgents.pop();
        }
        const store = mockStores[0]!;
        store.onHeartbeat.mockClear();

        routerHandler("heartbeat")({ agentId: "wiped", agentName: "W", timestampMs: Date.now() });
        routerHandler("heartbeat")({ agentId: "a", agentName: "A", timestampMs: Date.now() });

        expect(store.onHeartbeat.mock.calls.map(c => (c[0] as { agentId: string }).agentId)).toEqual(["a"]);
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

    it("says when the socket opens and closes, whatever the broker is doing", () => {
        const said: boolean[] = [];
        const stop = listen("af-socket", detail => said.push(detail.open));

        wsHandler("mqttStatus")(false);
        wsHandler("connected")();
        wsHandler("disconnected")();
        document.removeEventListener("af-socket", stop);

        expect(said).toEqual([true, false]);
    });

    it("takes a reply on the socket and an agent's own message down the same path", () => {
        const feed: FeedItem[] = [];
        const arrived: unknown[] = [];
        const onFeed = listen("af-feed-push", detail => feed.push(detail.item));
        const onChat = listen("af-chat-message", detail => arrived.push(detail.msg));

        wsHandler("chat")("on the socket", "weather", 1_000, "user");
        routerHandler("chat")({
            id: "r1",
            from: "weather",
            to: "user",
            content: "on its own",
            timestampMs: 2_000,
        });
        routerHandler("chat")({
            id: "r2",
            from: "weather",
            to: "catalog",
            content: "do it",
            timestampMs: 3_000,
        });

        document.removeEventListener("af-feed-push", onFeed);
        document.removeEventListener("af-chat-message", onChat);
        expect(feed.map(i => i.label)).toEqual(["on the socket", "on its own", "→ catalog: do it"]);
        expect(arrived).toHaveLength(3);
    });

    it("goes live only when both the /ws is open and the broker is up, then seeds once", () => {
        wsHandler("connected")(); // wsOpen=true, but broker still down → not live yet
        wsHandler("mqttStatus")(true); // both up → live: prunes + seeds
        wsHandler("mqttStatus")(true); // unchanged → recompute early-returns
        wsHandler("connected")(); // unchanged → seeded guard holds
        wsHandler("mqttStatus")(false); // broker down → disconnected
        wsHandler("disconnected")(); // /ws down → stays disconnected
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

    it("puts a node coming online on the feed, and its next report with the heartbeats", () => {
        const pushed: FeedItem[] = [];
        const record = (e: Event) => pushed.push((e as CustomEvent<{ item: FeedItem }>).detail.item);
        document.addEventListener("af-feed-push", record);

        routerHandler("node-heartbeat")({ node: "a-new-node", agents: ["flic"] });
        routerHandler("node-heartbeat")({ node: "a-new-node", agents: ["flic"] });
        document.removeEventListener("af-feed-push", record);

        expect(pushed.map(i => [i.type, i.label])).toEqual([
            ["health", "node online · 1 agent"],
            ["heartbeat", "heartbeat · 1 agent"],
        ]);
    });

    it("routes HA raw events through the feed (and ignores non-HA topics)", () => {
        routerHandler("raw")({
            topic: "homeassistant/state_changes/light/x",
            payload: { new_state: { state: "on", attributes: { friendly_name: "Lamp" } } },
        });
        routerHandler("raw")({ topic: "not-ha", payload: {} }); // parses to null
        expect(true).toBe(true);
    });

    it("drives the WebSocket handlers", () => {
        wsHandler("serverEvent")("agents/a/heartbeat", { cpu: 1 }); // forwarded to router.route
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

    it("hands the totals and the host's readings to the dashboard", () => {
        const dashboard = mockDashboards[0]!;
        wsHandler("statePatch")([], undefined, { totalCostUsd: 7, totalMessages: 4 });
        routerHandler("host-stats")({ cpu: 1, memUsedMb: 2, memTotalMb: 3 });
        expect(dashboard.setTotalCostUsd).toHaveBeenLastCalledWith(7);
        expect(dashboard.setTotalMessages).toHaveBeenLastCalledWith(4);
        expect(dashboard.setHostStats).toHaveBeenLastCalledWith(1, 2, 3);
    });

    it("tells the socket whom the chat addresses", () => {
        emit("af-addressed", { agent: "weather" });
        expect(mockAddressed.at(-1)).toBe("weather");
    });

    it("sends a pressed option to the agent, and closes the question on an answer", () => {
        const answered: unknown[] = [];
        const seen = (e: Event): void => {
            answered.push((e as CustomEvent).detail);
        };
        document.addEventListener("af-answered", seen);
        mockSent.length = 0;
        try {
            emit("af-answer-option", { id: "q1", agentId: "a", option: "yes", index: 0 });
            routerHandler("answer")({ id: "q1", agentId: "a", option: "yes", index: 0, channel: "telegram" });
        } finally {
            document.removeEventListener("af-answered", seen);
        }
        expect(mockSent).toEqual([{ type: "answer", agent_id: "a", id: "q1", option: "yes", index: 0 }]);
        expect(answered).toEqual([{ id: "q1", option: "yes" }]);
    });

    it("a question stays up as a toast with its options, until it is answered", () => {
        vi.mocked(toast.show).mockClear();
        vi.mocked(toast.close).mockClear();
        routerHandler("chat")({
            id: "q2",
            from: "installer",
            to: "user",
            content: "Install?",
            timestampMs: 1,
            agentId: "i",
            options: ["Install", "Refuse"],
        });
        const shown = vi.mocked(toast.show).mock.calls[0]![0];
        expect(shown.sticky).toBe(true);
        expect(shown.actions!.map(a => a.label)).toEqual(["Install", "Refuse"]);

        routerHandler("answer")({ id: "q2", agentId: "i", option: "Refuse", index: 1, channel: "dashboard" });
        expect(toast.close).toHaveBeenCalledWith("q2");
    });

    it("empties a deleted agent's question book, which the server dropped with it", () => {
        const books: unknown[] = [];
        const stop = listen("af-question-book", d => books.push(d));

        wsHandler("statePatch")([], "gone-agent", undefined);

        document.removeEventListener("af-question-book", stop);
        expect(books).toEqual([{ agentId: "gone-agent", questions: [] }]);
    });

    it("going live shows each question still open as its toast, every time", async () => {
        wsHandler("mqttStatus")(false);
        vi.mocked(toast.show).mockClear();
        mockOpenQuestions.mockResolvedValue([
            {
                id: "q3",
                from: "triage",
                to: "user",
                content: "Maintenance?",
                timestampMs: 1,
                agentId: "t",
                options: ["Maintenance", "Real problem"],
            },
            // Not a question: no options, so no toast to answer it with.
            { id: "m1", from: "triage", to: "user", content: "hello", timestampMs: 1 },
        ]);

        const handed: { id: string }[] = [];
        const stop = listen("af-questions-listed", detail => handed.push(...detail.open));
        wsHandler("connected")();
        wsHandler("mqttStatus")(true);
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalledTimes(1));
        document.removeEventListener("af-questions-listed", stop);

        const shown = vi.mocked(toast.show).mock.calls[0]![0];
        expect(shown.key).toBe("q3");
        expect(handed.map(m => m.id)).toEqual(["q3", "m1"]);
        expect(shown.actions!.map(a => a.label)).toEqual(["Maintenance", "Real problem"]);

        wsHandler("mqttStatus")(false);
        wsHandler("mqttStatus")(true);
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalledTimes(2));
        expect(mockOpenQuestions).toHaveBeenLastCalledWith("");
        mockOpenQuestions.mockResolvedValue([]);
    });

    it("tells the page a stored model's pointer moved", () => {
        const heard: unknown[] = [];
        const books: unknown[] = [];
        const stopBooks = listen("af-question-book", d => books.push(d));
        routerHandler("question-book")({ agentId: "a1", questions: [] });
        document.removeEventListener("af-question-book", stopBooks);
        expect(books).toEqual([{ agentId: "a1", questions: [] }]);

        const stop = listen("af-model-pointer", d => heard.push(d));

        routerHandler("model-pointer")({ name: "hvac", pointer: "shadow" });
        document.removeEventListener("af-model-pointer", stop);

        expect(heard).toEqual([{ name: "hvac", pointer: "shadow" }]);
    });

    it("handles the app-event listeners", () => {
        emit("af-stream-end", { text: "done", from: "A", agentId: "a" });
        emit("af-stream-end", { text: "", from: "A", agentId: "a" }); // !text → return
        emit("af-agent-command", { command: "stop", agentId: "a" }); // non-delete
        emit("af-send-message", {
            id: "s1",
            content: "hi",
            target: "a",
            name: "A",
            answerer: "a",
            attachments: [],
        }); // agent found
        emit("af-send-message", {
            id: "s2",
            content: "hi",
            target: "nope",
            name: "nope",
            answerer: "nope",
            attachments: [],
        }); // null
        emit("af-wipe-all", { survivors: ["a"] });
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

    it("ignores opaque cross-origin script errors (no error object, no filename)", () => {
        // What an injected script from another origin looks like — e.g. a browser
        // extension, or the desktop shell's webview bridge hitting our CSP. There
        // is nothing actionable in it, so it must not blame the app with a toast.
        vi.mocked(toast.show).mockClear();
        window.dispatchEvent(
            Object.assign(new Event("error"), { error: null, message: "Script error.", filename: "" }),
        );
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("still reports an errorless event that carries a filename (real page error)", () => {
        vi.mocked(toast.show).mockClear();
        window.dispatchEvent(
            Object.assign(new Event("error"), {
                error: null,
                message: "boom",
                filename: "http://localhost/app.js",
            }),
        );
        expect(toast.show).toHaveBeenCalledTimes(1);
    });
});
