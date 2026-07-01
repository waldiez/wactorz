/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wactorz Dashboard — entry point / composition root.
 *
 * This file only *wires*: it instantiates the services, derives the deployment
 * URLs, and connects transports → store → UI. Every decision/transform lives in
 * a tested module (agents/mapping, agents/deletionGuard, ui/haFeed,
 * ui/dashboard/haConfig); the handlers below are thin delegators. Keep it that
 * way — if a handler grows real logic, extract it (see CONTRIBUTING).
 *
 * The chat lives entirely in CardDashboard's in-card bar (DashboardChat), which
 * renders from the af-chat-message / af-stream-* events IOManager emits.
 *
 * Sections below run top-to-bottom in this order:
 *   1. Deployment env & service URLs   5. Wiring — Home Assistant feed
 *   2. Core services & shared state    6. Wiring — app events (CustomEvents)
 *   3. Helpers                         7. Startup — REST seed, TTS, connect
 *   4. Wiring — transports (WS, MQTT)  8. Teardown
 */

import "./app.css";
import { AgentStore } from "./agents/AgentStore";
import { MQTTClient } from "./mqtt/MQTTClient";
import { IOManager } from "./io/IOManager";
import { log } from "./io/logger";
import { emit, listen } from "./events";
import { WSChatClient } from "./io/WSChatClient";
import { tts } from "./io/TTSManager";
import { toast } from "./ui/ToastManager";
import { createHaFeedPusher, parseHaRawEvent } from "./ui/haFeed";
import { DropZone } from "./ui/DropZone";
import { UPLOADS_ENABLED } from "./ui/dashboard/uploads";
import type { AgentInfo } from "./types/agent";
import type { FeedItem } from "./types/feed";
import {
    toAgentInfo,
    reconcileActorList,
    buildNameIndex,
    selectLogFeedReplay,
    type LogFeedReplayState,
    buildMetricsUpdate,
    heartbeatFeedItem,
    spawnFeedItem,
    spawnTypeLabel,
    alertFeedItem,
    alertKind,
    chatFeedItem,
    feedSeedItem,
    stoppedFeedItem,
    qaFlagFeedItem,
    logFeedItem,
    completedFeedItem,
    nodeHeartbeatFeedItem,
} from "./agents/mapping";
import { createDeletionGuard } from "./agents/deletionGuard";

// ═══ 1 · Deployment env & service URLs ══════════════════════════════════════
//
// Two deployment contexts, both served same-origin:
//
//   1. HA addon       — __WACTORZ_INGRESS_PATH is injected by the Python
//                       server when the page is served behind HA's ingress
//                       proxy (e.g. /api/hassio_ingress/<slug>).
//   2. Direct browser / pywebview desktop — the monitor server serves this
//                       page, so relative URLs resolve correctly. The desktop
//                       shell loads http://127.0.0.1:<port>, i.e. same-origin.
//
// Never use window.location.host to build absolute URLs: inside the HAOS
// webview that host is the HA instance itself, not the addon backend.

// Clear any stale persisted theme from older builds.
localStorage.removeItem("wactorz-theme");

const _ingressPath: string = window.__WACTORZ_INGRESS_PATH ?? "";

// For fetch: ingress-prefixed under HA, plain-relative everywhere else.
const _apiBase = _ingressPath;

const _wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";

// For WebSocket: page host + ingress prefix.
const _wsHost = window.location.host;
const _wsBase = `${_wsProto}//${_wsHost}${_ingressPath}`;

// The MQTT WebSocket is always proxied at /mqtt on the *same* origin as this
// page — the monitor server serves both the page and the proxy on one port.
// So we derive the URL from window.location on every load and never persist it.
// This makes it impossible for a stale cached value (e.g. an old port like the
// hardcoded :8888) to break the connection and trip the "Demo fallback" badge.
//
// A build-time VITE_MQTT_WS_URL still wins for dev setups that talk to a broker
// directly. There is deliberately no per-user/runtime override: a same-origin
// proxy never needs one, and caching one in localStorage was the root cause of
// the stale-port failures.
const _mqttDefault = `${_wsBase}/mqtt`;

// Self-heal browsers that cached a URL under old builds (incl. the hardcoded :8888
// value). Removing it on load means existing users recover automatically on the
// next page load — no manual localStorage clearing required.
localStorage.removeItem("wactorz-mqtt-url");

const MQTT_BROKER = (import.meta.env["VITE_MQTT_WS_URL"] as string | undefined) || _mqttDefault;

// ═══ 2 · Core services & shared state ════════════════════════════════════════

const agentStore = new AgentStore();
const mqtt = new MQTTClient(MQTT_BROKER);
const ioManager = new IOManager(mqtt);
const wsChat = new WSChatClient();

// Global drag-and-drop upload overlay — only wired up when the backend endpoint
// exists (flip UPLOADS_ENABLED once /api/upload is live), so the overlay never
// appears for a feature that can't work yet.
const _dropZone = UPLOADS_ENABLED ? new DropZone(_apiBase) : null;

// Live-grid deletion guard (mirrors the backend's _deleted_agent_ids): blocks
// stale stop-window events from blinking a deleted card back, and re-admits a
// re-spawn via its newer timestamp. See createDeletionGuard for the rationale.
const { markDeleted, isDeleted } = createDeletionGuard();

const _logFeedState: LogFeedReplayState = { maxTs: 0, initialized: false };
let _mqttLive = false;
let liveSyncInFlight = false;
// Seed only once — MQTT reconnects must not re-add already-known agents.
let seeded = false;

// ═══ 3 · Helpers ═════════════════════════════════════════════════════════════

function pushFeed(item: FeedItem): void {
    emit("af-feed-push", { item });
}

function refreshLiveActors(): void {
    if (liveSyncInFlight) {
        return;
    }
    liveSyncInFlight = true;
    fetch(`${_apiBase}/api/actors`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((actors: AgentInfo[]) => {
            agentStore.reconcileAgents(reconcileActorList(actors, isDeleted));
            log.info(`[Dashboard] reconciled ${actors.length} live actors from REST`);
        })
        .catch(err => {
            // Dev mode without a running server is expected; log at debug so a
            // genuine backend failure still leaves a trace.
            log.debug("[Dashboard] live actor refresh failed:", err);
        })
        .finally(() => {
            liveSyncInFlight = false;
        });
}

// ═══ 4 · Wiring — WebSocket transport (chat replies · state patches · log feed)

// Non-streaming replies (slash commands, errors, one-shot agent replies)
wsChat.onChat((content, from, timestampMs) => {
    toast.show({ type: "chat", title: from, message: content.slice(0, 120) });
    const msg = {
        id: `ws-${timestampMs}`,
        from,
        to: "user",
        content,
        timestampMs,
    };
    ioManager.receiveAgentMessage(msg);
    agentStore.onChat(from, "user");
    const feedItem = {
        type: "chat" as const,
        label: content,
        agentName: from,
        timestamp: timestampMs,
    };
    emit("af-feed-push", { item: feedItem });
    emit("af-chat-message", { msg });
});

// Streaming replies — onStreamChunk / onStreamEnd are wired inside setWSClient
ioManager.setWSClient(wsChat);

// State patches broadcast by the server over the same /ws connection.
// This is how pause/stop/resume state changes reach the UI without polling.
wsChat.onStatePatch((agents, deletedId, stats) => {
    if (deletedId) {
        markDeleted(deletedId);
        agentStore.removeAgent(deletedId);
    }
    if (stats?.totalCostUsd !== undefined) {
        agentStore.setTotalCostUsd(stats.totalCostUsd);
    }
    if (stats?.totalMessages !== undefined) {
        agentStore.setTotalMessages(stats.totalMessages);
    }
    agents.forEach(a => {
        if (!a.agent_id || isDeleted(a.agent_id)) {
            return;
        }
        agentStore.addOrUpdateAgent(toAgentInfo(a));
    });
});

// The server embeds its in-memory log_feed (spawned/status/logs/alerts) in
// every state-patch.  We use this as a reliable secondary path so MQTT events
// appear in the feed even when the direct Mosquitto WebSocket is unavailable.
wsChat.onLogFeed(items => {
    // Nameless entries (e.g. `log`) borrow their friendly name from the
    // `spawned` entry in the same batch, then from the live agent store — so
    // reloads attribute them by name instead of a raw id.
    const nameIndex = buildNameIndex(items);
    const resolveName = (id: string): string | undefined =>
        nameIndex.get(id) ?? agentStore.getAgents().find(a => a.id === id)?.name;
    // Replay/dedup bookkeeping (first-batch backlog vs. high-water mark vs.
    // skip-while-MQTT-live) lives in selectLogFeedReplay so it stays tested.
    selectLogFeedReplay(items, _logFeedState, _mqttLive, resolveName).forEach(pushFeed);
});

// ═══ 4 · Wiring — MQTT transport ═════════════════════════════════════════════

mqtt.on("heartbeat", payload => {
    if (isDeleted(payload.agentId, payload.timestampMs)) {
        return;
    }
    agentStore.onHeartbeat(payload);
    pushFeed(heartbeatFeedItem(payload));
});

mqtt.on("spawn", payload => {
    // Block a stale spawn from the stop-window; a respawn carries a newer
    // timestamp and is re-admitted by isDeleted().
    if (isDeleted(payload.agentId, payload.timestampMs)) {
        return;
    }
    agentStore.onSpawn(payload);
    pushFeed(spawnFeedItem(payload));
    toast.show({
        type: "spawn",
        title: payload.agentName,
        message: `${spawnTypeLabel(payload)} is online`,
    });
});

mqtt.on("alert", payload => {
    agentStore.onAlert(payload);
    pushFeed(alertFeedItem(payload));
    toast.show({
        type: alertKind(payload.severity),
        title: payload.agentName ?? "system",
        message: (payload.message ?? "").slice(0, 120),
    });
});

mqtt.on("chat", msg => {
    if (msg.from !== "user") {
        toast.show({ type: "chat", title: msg.from, message: msg.content.slice(0, 120) });
    }
    ioManager.receiveAgentMessage(msg);
    agentStore.onChat(msg.from, msg.to);
    emit("af-chat-message", { msg });
    pushFeed(chatFeedItem(msg));
});

mqtt.on("status", payload => {
    if (!isDeleted(payload.agentId)) {
        agentStore.addOrUpdateAgent({
            id: payload.agentId,
            name: payload.agentName,
            state: payload.state,
            protected: payload.protected ?? false,
            messagesProcessed: payload.messagesProcessed,
        });
    }
    if (payload.state === "stopped") {
        window.setTimeout(() => refreshLiveActors(), 200);
        pushFeed(stoppedFeedItem(payload));
    }
});

mqtt.on("connected", () => {
    _mqttLive = true;
    log.info("[Dashboard] MQTT connected");
    emit("af-connection-status", { status: "live" });

    agentStore.pruneStaleRemoteAgents();

    if (seeded) {
        return;
    }
    seeded = true;

    // Startup spawn events are published before the browser connects.
    // Fetch the current actor list from REST so they appear immediately.
    refreshLiveActors();
});

mqtt.on("qa-flag", payload => {
    pushFeed(qaFlagFeedItem(payload));
});

mqtt.on("metrics", payload => {
    // Merge cost/message metrics into the agent record so dashboards can display them.
    const existing = agentStore.getAgents().find(a => a.id === payload.agentId);
    if (!existing) {
        return;
    }
    agentStore.addOrUpdateAgent(buildMetricsUpdate(payload, existing));
});

mqtt.on("logs", payload => {
    const item = logFeedItem(payload);
    if (item) {
        pushFeed(item);
    }
});

mqtt.on("completed", payload => {
    pushFeed(completedFeedItem(payload));
});

mqtt.on("node-heartbeat", payload => {
    agentStore.updateRemoteNode(payload.node, payload.agents);
    pushFeed(nodeHeartbeatFeedItem(payload));
});

mqtt.on("host-stats", stats => {
    if (stats.cpu !== undefined || stats.memUsedMb !== undefined) {
        agentStore.setHostStats(stats.cpu ?? 0, stats.memUsedMb ?? 0, stats.memTotalMb);
    }
});

mqtt.on("disconnected", () => {
    _mqttLive = false;
    log.warn("[Dashboard] MQTT disconnected");
    emit("af-connection-status", { status: "demo" });
});

mqtt.on("error", err => {
    log.error("[Dashboard] MQTT error:", err);
});

// ═══ 5 · Wiring — Home Assistant feed ════════════════════════════════════════
// HA entity state reaches the activity feed via the ha-state-bridge-agent over
// MQTT (ha/state/{domain}/{entity_id}). The browser no longer talks to HA
// directly — the Devices nav button just links out to the HA UI.
const pushHaFeed = createHaFeedPusher(pushFeed);

mqtt.on("raw", ({ topic, payload }) => {
    const ev = parseHaRawEvent(topic, payload);
    if (ev) {
        pushHaFeed(ev.entityId, ev.state, ev.friendlyName);
    }
});

// ═══ 6 · Wiring — app events (CustomEvents, see events.ts AppEventMap) ═══════

// Streaming reply finished — notify
listen("af-stream-end", detail => {
    const { text, from } = detail;
    if (!text) {
        return;
    }
    toast.show({ type: "chat", title: from, message: text.slice(0, 120) });
});

// Agent commands from CardDashboard → WebSocket
listen("af-agent-command", detail => {
    const { command, agentId } = detail;
    if (command === "delete") {
        // Mark deleted immediately so MQTT "stopped" events don't re-add the card
        // before the WS state-patch reply arrives.
        markDeleted(agentId);
        agentStore.removeAgent(agentId);
    }
    wsChat.sendRaw({ type: "command", command, agent_id: agentId });
});

// af-iobar sends: route through ioManager (same as regular io-bar)
listen("af-send-message", detail => {
    const { content } = detail;
    const agent = agentStore.getAgents().find(a => a.name === detail.target) ?? null;
    void ioManager.send(content, agent);
});

// wipe all
listen("af-wipe-all", () => {
    agentStore.clearAll();
    _logFeedState.maxTs = 0;
});

// A scoped reset (metrics / logs) cleared the server-side activity log — drop
// the on-screen feed too, since onLogFeed only ever appends and would otherwise
// keep showing stale lines until the next event.
listen("af-clear-feed", () => {
    _logFeedState.maxTs = 0;
});

// ═══ 7 · Startup — REST seed, TTS probe, connect ═════════════════════════════

wsChat.connect(`${_wsBase}/ws`);
refreshLiveActors();
const _liveActorsTimer = window.setInterval(() => {
    refreshLiveActors();
    agentStore.pruneStaleRemoteAgents();
}, 15000);

// Seed the activity feed from SQLite chat_log so the feed view isn't empty
// after a server restart (the server returns Unix seconds; feedSeedItem → ms).
fetch(`${_apiBase}/api/feed`)
    .then(r => (r.ok ? r.json() : []))
    .then(
        (items: { type: string; label: string; agentName: string; timestamp?: number; role?: string }[]) => {
            log.info("[feed] /api/feed seed:", items.length, "items");
            items.forEach(item => pushFeed(feedSeedItem(item)));
        },
    )
    .catch(err => log.debug("[feed] /api/feed seed failed:", err));

// The HA URL is seeded from /api/config by CardDashboard (see ui/dashboard/haConfig)
// for the Devices link; no token ever reaches the browser. The MQTT URL is
// likewise never seeded: the frontend always uses the same-origin /mqtt proxy
// (see _mqttDefault), so the broker address never belongs in the browser.

// Probe server TTS availability + load voice list (base must be set first so
// the request stays inside the ingress prefix instead of bare "/api").
tts.setApiBase(_apiBase);
tts.init();

mqtt.connect();

// ═══ 8 · Teardown ════════════════════════════════════════════════════════════

window.addEventListener("beforeunload", () => {
    window.clearInterval(_liveActorsTimer);
    mqtt.disconnect();
    wsChat.disconnect();
    agentStore.dispose();
});
