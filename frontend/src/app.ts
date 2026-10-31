/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard's composition root.
 *
 * This file only *wires*: it builds the services and connects transports →
 * store → UI. Every decision/transform lives in a tested module; the handlers
 * below are thin delegators. Keep it that way — if a handler grows real logic,
 * extract it (see CONTRIBUTING).
 *
 * The agents live in AgentStore, which CardDashboard observes; the chat lives
 * entirely in CardDashboard's in-card bar (DashboardChat), which renders from
 * the af-chat-message / af-stream-* events IOManager emits.
 *
 * `createApp` builds and wires everything, `start` connects it, and `stop` lets
 * it all go. The parts can be replaced, so a test builds a page of its own.
 */
import { apiUrl, ingressBase, requestJson } from "./io/api";
import { safeStorage } from "./safeStorage";
import { AgentStore } from "./agents/AgentStore";
import { ServerEventRouter } from "./io/ServerEventRouter";
import { ambient } from "./io/AmbientManager";
import { IOManager } from "./io/IOManager";
import { log } from "./io/logger";
import { Connection } from "./io/connection";
import { fetchQuestionLedger } from "./io/openQuestions";
import { emit, Listeners } from "./events";
import { WSClient } from "./io/WSClient";
import { register as registerTTS } from "./ext/tts";
import { loadServerConfig } from "./config/serverConfig";
import { installSessionExpiry } from "./io/sessionExpiry";
import { toast } from "./ui/ToastManager";
import { chatIngress } from "./ui/chatIngress";
import { QuestionToasts } from "./ui/questionToast";
import { createHaFeedPusher, parseHaRawEvent } from "./ui/haFeed";
import { DropZone } from "./ui/DropZone";
import { CardDashboard } from "./ui/CardDashboard";
import { uploadsEnabled } from "./ui/dashboard/uploads";
import type { ChatMessage } from "./types/agent";
import type { FeedItem } from "./types/feed";
import type { TTSConfig } from "./ext/tts";
import {
    toAgentInfo,
    socketMessage,
    buildNameIndex,
    selectLogFeedReplay,
    type LogFeedReplayState,
    buildMetricsUpdate,
    heartbeatFeedItem,
    spawnFeedItem,
    spawnTypeLabel,
    alertFeedItem,
    alertKind,
    answerFeedItem,
    feedSeedItem,
    stoppedFeedItem,
    qaFlagFeedItem,
    logFeedItem,
    completedFeedItem,
    nodeHeartbeatFeedItem,
    rawFeedItem,
} from "./agents/mapping";
import { ActorSync } from "./agents/actorSync";
import { createDeletionGuard, tombstoneWiped, type DeletionGuard } from "./agents/deletionGuard";
import { NodePresence } from "./agents/nodePresence";
import { startRouter } from "./router";
import type { QuestionLedger } from "./io/openQuestions";
import { STALE_MS } from "./agents/liveness";

// Two deployment contexts, both served same-origin:
//
//   1. HA addon       — __WACTORZ_INGRESS_PATH is injected by the Python
//                       server when the page is served behind HA's ingress
//                       proxy (e.g. /api/hassio_ingress/<slug>).
//   2. Direct browser — the monitor server serves this page itself, so
//                       relative URLs resolve correctly.
//
// Never use window.location.host to build absolute URLs: inside the HAOS
// webview that host is the HA instance itself, not the addon backend.

/** What the app is built from; a test replaces what it wants to see or drive. */
export interface AppParts {
    /** The socket to the server. */
    ws: () => WSClient;
    /** Decodes what arrives on it. */
    router: () => ServerEventRouter;
    /** Holds the agents. */
    store: () => AgentStore;
    /** The page. */
    dashboard: (store: AgentStore) => CardDashboard;
    /** Chat over the socket. */
    ioManager: (router: ServerEventRouter) => IOManager;
    /** Watches for an ended session; returns how to stop. */
    installSessionExpiry: (onRevoked: () => void) => () => void;
    /** Reads the open and answered questions. */
    readLedger: (apiBase: string) => Promise<QuestionLedger>;
    /** Boots the voice. */
    registerTTS: (config: TTSConfig) => () => void;
    /** The window-wide drop target for uploads, with how to remove it. */
    dropZone: (apiBase: string) => { destroy(): void };
}

const REAL_PARTS: AppParts = {
    ws: () => new WSClient(),
    router: () => new ServerEventRouter(),
    store: () => new AgentStore(),
    dashboard: store => new CardDashboard(store),
    ioManager: router => new IOManager(router),
    installSessionExpiry: onRevoked => installSessionExpiry(window, onRevoked),
    readLedger: fetchQuestionLedger,
    registerTTS,
    dropZone: apiBase => new DropZone(apiBase),
};

/** The running page. */
export interface App {
    /** Connect, and read what the page starts from. */
    start(): void;
    /** Let everything go. Safe to call twice. */
    stop(): void;
}

/** What the wiring below shares. */
interface AppWiring {
    ws: WSClient;
    router: ServerEventRouter;
    store: AgentStore;
    dashboard: CardDashboard;
    ioManager: IOManager;
    nodePresence: NodePresence;
    isDeleted: DeletionGuard["isDeleted"];
    markDeleted: DeletionGuard["markDeleted"];
    logFeed: LogFeedReplayState;
    actorSync: ActorSync;
    connection: Connection;
    ingestChat: (msg: ChatMessage) => void;
    listeners: Listeners;
    pushFeed: (item: FeedItem) => void;
}

/** Surface otherwise-silent failures to the log and a toast; without this they
 *  reach only the console. */
function reportGlobalError(context: string, detail: unknown): void {
    log.error(`[${context}]`, detail);
    toast.show({
        type: "alert-error",
        title: "Unexpected error",
        message: "Something went wrong — see the console for details.",
    });
}

/** Report uncaught exceptions and unhandled promise rejections until `signal` aborts. */
function wireGlobalErrors(signal: AbortSignal): void {
    window.addEventListener(
        "error",
        e => {
            // Scripts injected from another origin (browser extensions, an embedding
            // webview's JS bridge) report as an opaque "Script error." with no error
            // object, filename or line — nothing anyone can act on. Toasting those
            // blames the app for someone else's script, so only report real page errors.
            if (!e.error && !e.filename) {
                return;
            }
            reportGlobalError("uncaught", e.error ?? e.message);
        },
        { signal },
    );
    window.addEventListener("unhandledrejection", e => reportGlobalError("unhandledrejection", e.reason), {
        signal,
    });
}

/** Chat replies, state patches and the log feed, from the socket. */
function wireSocket(app: AppWiring): void {
    // Non-streaming replies (slash commands, errors, one-shot agent replies)
    app.ws.on("chat", (...frame) => app.ingestChat(socketMessage(...frame)));

    // Streaming replies — onStreamChunk / onStreamEnd are wired inside setWSClient
    app.ioManager.setWSClient(app.ws);

    // State patches broadcast by the server over the same /ws connection.
    // This is how start/stop state changes reach the UI without polling.
    app.ws.on("statePatch", (agents, deletedId, stats) => {
        if (deletedId) {
            app.markDeleted(deletedId);
            app.store.removeAgent(deletedId);
            // The server dropped the agent's book with it, and a withdrawn manifest clears
            // no book. Here and not when the store drops an agent: a silent node's agents
            // leave the store too, and what they asked still waits.
            emit("af-question-book", { agentId: deletedId, questions: [] });
        }
        if (stats?.totalCostUsd !== undefined) {
            app.dashboard.setTotalCostUsd(stats.totalCostUsd);
        }
        if (stats?.totalMessages !== undefined) {
            app.dashboard.setTotalMessages(stats.totalMessages);
        }
        agents.forEach(a => {
            if (!a.agent_id || app.isDeleted(a.agent_id)) {
                return;
            }
            app.store.addOrUpdateAgent(toAgentInfo(a));
        });
    });

    // The server embeds its in-memory log_feed (spawned/status/logs/alerts) in
    // every state-patch.  We use this as a reliable secondary path so MQTT events
    // appear in the feed even when the direct Mosquitto WebSocket is unavailable.
    app.ws.on("logFeed", items => {
        // Nameless entries (e.g. `log`) borrow their friendly name from the
        // `spawned` entry in the same batch, then from the live agent store — so
        // reloads attribute them by name instead of a raw id.
        const nameIndex = buildNameIndex(items);
        const resolveName = (id: string): string | undefined => nameIndex.get(id) ?? app.store.get(id)?.name;
        // Replay/dedup bookkeeping (first-batch backlog vs. high-water mark vs.
        // skip-while-ws-live) lives in selectLogFeedReplay so it stays tested.
        selectLogFeedReplay(items, app.logFeed, app.connection.live, resolveName).forEach(app.pushFeed);
    });
}

/** What agents say of themselves: they start and beat. */
function wireAgentEvents(app: AppWiring): void {
    app.router.on("heartbeat", payload => {
        if (app.isDeleted(payload.agentId, payload.timestampMs)) {
            return;
        }
        app.store.onHeartbeat(payload);
        app.pushFeed(heartbeatFeedItem(payload));
    });

    app.router.on("spawn", payload => {
        // Block a stale spawn from the stop-window; a respawn carries a newer
        // timestamp and is re-admitted by isDeleted().
        if (app.isDeleted(payload.agentId, payload.timestampMs)) {
            return;
        }
        app.store.onSpawn(payload);
        app.pushFeed(spawnFeedItem(payload));
        toast.show({
            type: "spawn",
            title: payload.agentName,
            message: `${spawnTypeLabel(payload)} is online`,
        });
    });

    app.router.on("alert", payload => {
        app.store.onAlert(payload);
        app.pushFeed(alertFeedItem(payload));
        toast.show({
            type: alertKind(payload.severity),
            title: payload.agentName ?? "system",
            message: (payload.message ?? "").slice(0, 120),
        });
    });

    app.router.on("chat", msg => app.ingestChat(msg));
}

/** What else agents say: their state, answers and alerts. */
function wireAgentChanges(app: AppWiring): void {
    // A stored model's pointer moved: the Models view reads it again.
    app.router.on("model-pointer", moved => emit("af-model-pointer", moved));

    // An agent's question book changed: the Inbox follows it.
    app.router.on("question-book", book => emit("af-question-book", book));

    // A question answered on any channel: the card closes on every open dashboard.
    app.router.on("answer", answer => {
        emit("af-answered", { id: answer.id, option: answer.option });
        toast.close(answer.id);
        app.pushFeed(answerFeedItem(answer));
    });

    app.router.on("status", payload => {
        if (!app.isDeleted(payload.agentId)) {
            app.store.addOrUpdateAgent({
                id: payload.agentId,
                name: payload.agentName,
                state: payload.state,
                protected: payload.protected ?? false,
                messagesProcessed: payload.messagesProcessed,
            });
        }
        if (payload.state === "stopped") {
            window.setTimeout(() => void app.actorSync.refresh(), 200);
            app.pushFeed(stoppedFeedItem(payload));
        }
    });
}

/** The socket's own state, and the frames it hands the router. */
function wireConnection(app: AppWiring): void {
    // The /ws transport owns the connection; the router only decodes events.
    app.ws.on("serverEvent", (topic, payload, threadId) => app.router.route(topic, payload, threadId));
    app.ws.on("connected", () => app.connection.socket(true));
    app.ws.on("disconnected", () => app.connection.socket(false));
    app.ws.on("mqttStatus", up => app.connection.broker(up));
}

/** Readings: metrics, logs, nodes and the host. */
function wireTelemetry(app: AppWiring): void {
    app.router.on("qa-flag", payload => {
        app.pushFeed(qaFlagFeedItem(payload));
    });

    app.router.on("metrics", payload => {
        // Merge cost/message metrics into the agent record so dashboards can display them.
        const existing = app.store.get(payload.agentId);
        if (!existing) {
            return;
        }
        app.store.addOrUpdateAgent(buildMetricsUpdate(payload, existing));
    });

    app.router.on("logs", payload => {
        const item = logFeedItem(payload);
        if (item) {
            app.pushFeed(item);
        }
    });

    app.router.on("completed", payload => {
        app.pushFeed(completedFeedItem(payload));
    });

    app.router.on("node-heartbeat", payload => {
        app.store.updateRemoteNode(payload.node, payload.agents, payload.readings);
        app.pushFeed(nodeHeartbeatFeedItem(payload, app.nodePresence.heard(payload.node)));
    });

    app.router.on("host-stats", stats => {
        if (stats.cpu !== undefined || stats.memUsedMb !== undefined) {
            app.dashboard.setHostStats(stats.cpu ?? 0, stats.memUsedMb ?? 0, stats.memTotalMb);
        }
    });
}

/** Home Assistant's entities and other feed-only topics. */
function wireRawFeed(app: AppWiring): void {
    // HA entity state reaches the activity feed via the ha-state-bridge-agent over
    // WS (homeassistant/state_changes/{domain}/{entity_id}). The browser no longer
    // talks to HA directly — the Devices nav button just links out to the HA UI.
    const pushHaFeed = createHaFeedPusher(app.pushFeed);

    app.router.on("raw", ({ topic, payload }) => {
        const ev = parseHaRawEvent(topic, payload);
        if (ev) {
            pushHaFeed(ev.entityId, ev.state, ev.friendlyName);
            return;
        }
        // Other feed-only agent topics (actuations, anomaly, …) via the extensible
        // registry in mapping.ts — resolve the friendly name from the live store.
        const item = rawFeedItem(topic, payload, id => app.store.get(id)?.name);
        if (item) {
            app.pushFeed(item);
        }
    });
}

/** What a person does on the page, on its way to the server. */
function wirePageActions(app: AppWiring): void {
    // Streaming reply finished — notify
    app.listeners.on("af-stream-end", detail => {
        const { text, from } = detail;
        if (!text) {
            return;
        }
        toast.show({ type: "chat", title: from, message: text.slice(0, 120) });
    });

    // Agent commands from CardDashboard → WebSocket
    app.listeners.on("af-agent-command", detail => {
        const { command, agentId } = detail;
        if (command === "delete") {
            // Mark deleted immediately so MQTT "stopped" events don't re-add the card
            // before the WS state-patch reply arrives.
            app.markDeleted(agentId);
            app.store.removeAgent(agentId);
        }
        app.ws.sendRaw({ type: "command", command, agent_id: agentId });
    });

    // A press on one of a question's buttons: the pick goes to the agent that
    // asked, over the server's broker link, as a tap on Telegram would.
    app.listeners.on("af-answer-option", detail => {
        toast.close(detail.id);
        app.ws.sendRaw({
            type: "answer",
            agent_id: detail.agentId,
            id: detail.id,
            option: detail.option,
            index: detail.index,
        });
    });
}

/** Sends, and what a wipe or a reset clears. */
function wirePageResets(app: AppWiring): void {
    // The chat says who is addressed; the socket attributes the gateway's replies to it.
    app.listeners.on("af-addressed", ({ agent }) => app.ws.addressed(agent));

    // af-iobar sends: route through ioManager (same as regular io-bar)
    app.listeners.on("af-send-message", detail => {
        const { id, content, attachments } = detail;
        // The thread's agent, by its id: the send names it to the server by name and id.
        const agent = app.store.get(detail.target) ?? null;
        void app.ioManager.send(content, agent, attachments, id, agent ? app.store.label(agent) : "");
    });

    // wipe all
    app.listeners.on("af-wipe-all", ({ survivors }) => {
        tombstoneWiped(app.store.list(), survivors, app.markDeleted);
        app.store.clearAll();
        app.logFeed.maxTs = 0;
    });

    // A scoped reset (metrics / logs) cleared the server-side activity log — drop
    // the on-screen feed too, since onLogFeed only ever appends and would otherwise
    // keep showing stale lines until the next event.
    app.listeners.on("af-clear-feed", () => {
        app.logFeed.maxTs = 0;
    });
}

/** Forget what older builds left in the browser. */
function clearOldKeys(): void {
    // Clear any stale persisted theme from older builds.
    safeStorage.remove("wactorz-theme");
    // Self-heal browsers that cached a URL under old builds (incl. the hardcoded :8888
    // value). Removing it on load means existing users recover automatically on the
    // next page load — no manual localStorage clearing required.
    safeStorage.remove("wactorz-mqtt-url");
}

/** The socket's address: page host + ingress prefix. */
function socketUrl(): string {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}${ingressBase()}/ws`;
}

/** The services over `ws` and `router`, built in the order they depend on. */
function buildServices(parts: AppParts, ws: WSClient, router: ServerEventRouter): AppWiring {
    const store = parts.store();
    const dashboard = parts.dashboard(store);
    dashboard.show();
    const ioManager = parts.ioManager(router);
    // Live-grid deletion guard (mirrors the backend's _deleted_agent_ids): blocks
    // stale stop-window events from blinking a deleted card back, and re-admits a
    // re-spawn via its newer timestamp. See createDeletionGuard for the rationale.
    const { markDeleted, isDeleted } = createDeletionGuard();
    const actorSync = new ActorSync(store, isDeleted);
    const questions = new QuestionToasts(() => dashboard.inboxOpen);
    const connection = new Connection(() => {
        store.pruneSilentRemoteAgents();
        void actorSync.seedOnce();
        // A question asked while the page was closed, or the socket down, reached
        // neither a toast nor its thread: the ledger has it.
        void parts.readLedger(ingressBase()).then(ledger => {
            emit("af-questions-listed", ledger);
            questions.listed(ledger);
        });
    });
    return {
        ws,
        router,
        store,
        dashboard,
        ioManager,
        // Online by the overview's measure, so the feed and the node cards agree.
        nodePresence: new NodePresence(STALE_MS),
        isDeleted,
        markDeleted,
        logFeed: { maxTs: 0, initialized: false },
        actorSync,
        connection,
        ingestChat: chatIngress({ speaker: ioManager, store, questions }),
        listeners: new Listeners(),
        pushFeed: item => emit("af-feed-push", { item }),
    };
}

/** Connect every handler. */
function wireAll(app: AppWiring): void {
    wireSocket(app);
    wireAgentEvents(app);
    wireAgentChanges(app);
    wireConnection(app);
    wireTelemetry(app);
    wireRawFeed(app);
    wirePageActions(app);
    wirePageResets(app);
}

/** What `start` sets going, and `stop` has to end. */
interface Running {
    /** Ends what the voice listens to, once it has booted. */
    stopVoice: (() => void) | null;
    /** The poll of the actor list; clearing 0 does nothing. */
    actorPoll: number;
    /** Global drag-and-drop upload overlay. Built after /api/config answers, since
     *  only the server knows whether the upload routes are registered — an overlay
     *  raised before that would accept a file it has nowhere to send. */
    dropZone: { destroy(): void } | null;
    stopped: boolean;
}

/** Read the list on a timer, while the page is looked at. */
function pollActors(app: AppWiring): number {
    return window.setInterval(() => {
        if (document.hidden) {
            return;
        }
        void app.actorSync.refresh();
        app.store.pruneSilentRemoteAgents();
    }, 15000);
}

/** Read the server's config, then boot what depends on it. */
function bootFromConfig(parts: AppParts, running: Running): void {
    // The HA URL is seeded from /api/config at startup (see config/serverConfig.ts);
    // the Devices nav link reads it from safeStorage. No token ever reaches the
    // browser. The broker address isn't seeded either — the browser never connects
    // to MQTT; it gets everything over /ws.

    // Seed server config early so TTS availability is known before probing.
    void loadServerConfig().then(read => {
        // A page stopped while this was on its way has nothing to boot.
        if (running.stopped) {
            return;
        }
        // Boot TTS after config is seeded: what it says decides whether to probe.
        running.stopVoice = parts.registerTTS({ apiBase: ingressBase(), configRead: read });
        // Same reason, same moment: the server has now said whether it takes
        // uploads at all.
        if (uploadsEnabled()) {
            running.dropZone = parts.dropZone(ingressBase());
        }
    });
}

// `beforeunload` does not fire reliably on mobile, where a page is more often
// frozen than unloaded — so the socket was left for the server to time out.
// `pagehide` does fire, and its `persisted` flag distinguishes the two: a
// frozen page may be restored from the bfcache and must keep working, so only
// a real unload tears down. Both paths land in the same idempotent function.
function stopOnUnload(stop: () => void, signal: AbortSignal): void {
    window.addEventListener(
        "pagehide",
        e => {
            if (!e.persisted) {
                stop();
            }
        },
        { signal },
    );
    window.addEventListener("beforeunload", stop, { signal });
}

/** Build the app and wire everything, ready to `start`. */
export function createApp(replaced: Partial<AppParts> = {}): App {
    const parts = { ...REAL_PARTS, ...replaced };
    clearOldKeys();
    const aborting = new AbortController();
    wireGlobalErrors(aborting.signal);
    const router = parts.router();
    const ws = parts.ws();
    const running: Running = { stopVoice: null, actorPoll: 0, dropZone: null, stopped: false };

    // Ahead of the first request this makes: mounting the dashboard starts its
    // polling. A session that ended while the tab was closed refuses that one
    // too, and installing after it would leave the very first 401 unhandled and the
    // page waiting on a poll half a minute away.
    const stopWatching = parts.installSessionExpiry(() => {
        // The socket is the one thing that would keep trying: it reconnects on a
        // timer, and every attempt goes to the same revoked prefix.
        ws.disconnect();
        window.clearInterval(running.actorPoll);
    });

    // Before the dashboard mounts: the first view it opens sets the address.
    const stopRouter = startRouter();
    const app = buildServices(parts, ws, router);
    wireAll(app);
    const stopDucking = ambient.followSpeech();

    function start(): void {
        ws.connect(socketUrl());
        void app.actorSync.refresh();
        running.actorPoll = pollActors(app);
        seedFeed(app.pushFeed);
        bootFromConfig(parts, running);
    }

    function stop(): void {
        if (running.stopped) {
            return;
        }
        running.stopped = true;
        window.clearInterval(running.actorPoll);
        ws.disconnect();
        app.dashboard.destroy();
        stopRouter();
        // Detaches the window drag listeners it put up; null when uploads are off.
        running.dropZone?.destroy();
        running.dropZone = null;
        app.listeners.off();
        running.stopVoice?.();
        stopDucking();
        stopWatching();
        aborting.abort();
    }

    stopOnUnload(stop, aborting.signal);
    return { start, stop };
}

/** The feed items the server keeps, as `/api/feed` gives them. */
type StoredFeedItem = { type: string; label: string; agentName: string; timestamp?: number; role?: string };

/** Seed the activity feed from SQLite chat_log so the feed view isn't empty
 *  after a server restart (the server returns Unix seconds; feedSeedItem → ms). */
function seedFeed(pushFeed: (item: FeedItem) => void): void {
    requestJson<StoredFeedItem[]>(apiUrl("/api/feed"))
        // Reject rather than substitute []: a 500 became "no history" indistinguishable
        // from an empty log, silently and with nothing written anywhere.
        .then(got => (got.ok ? got.value : Promise.reject(new Error(got.error))))
        .then(items => {
            log.info("[feed] /api/feed seed:", items.length, "items");
            items.forEach(item => pushFeed(feedSeedItem(item)));
        })
        // warn, not debug: this runs once, so there is no run of failures to
        // escalate from, and the user-visible result is an empty feed with no
        // explanation. debug is dropped in a production build.
        .catch(err => log.warn("[feed] /api/feed seed failed — feed starts empty:", err));
}
