/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wactorz Dashboard — entry point.
 *
 * Bootstrap order:
 * 1. Create AgentStore (agent-state store + CardDashboard coordinator)
 * 2. Create MQTTClient and connect to broker
 * 3. Wire MQTT events → AgentStore (which drives the cards dashboard)
 *
 * The chat lives entirely in CardDashboard's in-card bar (DashboardChat),
 * which renders from the af-chat-message / af-stream-* events IOManager emits.
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
import { resolveAgentName } from "./agents/naming";
import {
    toAgentInfo,
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
    stoppedFeedItem,
    qaFlagFeedItem,
    logFeedItem,
    completedFeedItem,
    nodeHeartbeatFeedItem,
} from "./agents/mapping";
import { createDeletionGuard } from "./agents/deletionGuard";

const agentStore = new AgentStore();

// Clear any stale persisted theme from older builds.
localStorage.removeItem("wactorz-theme");

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
const mqtt = new MQTTClient(MQTT_BROKER);

const ioManager = new IOManager(mqtt);

// Global drag-and-drop upload overlay — only wired up when the backend endpoint
// exists (flip UPLOADS_ENABLED once /api/upload is live), so the overlay never
// appears for a feature that can't work yet.
const _dropZone = UPLOADS_ENABLED ? new DropZone(_apiBase) : null;

const wsChat = new WSChatClient();
let liveSyncInFlight = false;
// Live-grid deletion guard (mirrors the backend's _deleted_agent_ids): blocks
// stale stop-window events from blinking a deleted card back, and re-admits a
// re-spawn via its newer timestamp. See createDeletionGuard for the rationale.
const { markDeleted, isDeleted } = createDeletionGuard();

function refreshLiveActors(): void {
    if (liveSyncInFlight) {
        return;
    }
    liveSyncInFlight = true;
    fetch(`${_apiBase}/api/actors`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((actors: AgentInfo[]) => {
            agentStore.reconcileAgents(
                actors
                    .filter(a => !isDeleted(a.id))
                    .map(a => ({
                        ...a,
                        name: resolveAgentName(a.name, a.id),
                    })),
            );
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

wsChat.connect(`${_wsBase}/ws`);
refreshLiveActors();
window.setInterval(() => {
    refreshLiveActors();
    agentStore.pruneStaleRemoteAgents();
}, 15000);

// The server embeds its in-memory log_feed (spawned/status/logs/alerts) in
// every state-patch.  We use this as a reliable secondary path so MQTT events
// appear in the feed even when the direct Mosquitto WebSocket is unavailable.

const _logFeedState: LogFeedReplayState = { maxTs: 0, initialized: false };
let _mqttLive = false;

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

// Seed the activity feed from SQLite chat_log so the feed view isn't empty
// after a server restart. The server returns real Unix timestamps (seconds);
// convert to ms for the feed.
fetch(`${_apiBase}/api/feed`)
    .then(r => (r.ok ? r.json() : []))
    .then(
        (items: { type: string; label: string; agentName: string; timestamp?: number; role?: string }[]) => {
            log.info("[feed] /api/feed seed:", items.length, "items");
            if (!items.length) {
                return;
            }
            items.forEach(item => {
                pushFeed({
                    type: "chat",
                    label: item.label,
                    agentName: item.agentName,
                    timestamp: item.timestamp ? item.timestamp * 1000 : Date.now(),
                    role: item.role,
                });
            });
        },
    )
    .catch(err => log.debug("[feed] /api/feed seed failed:", err));

// Backend config (.env) is the source of truth. We track the last server value
// we seeded (key + "__server") so we can tell "the user edited this locally"
// from "the .env value changed". When the server value changes we update the
// active key; otherwise a genuine user edit in Settings survives reloads.
//
// Note: the MQTT URL is intentionally NOT seeded here. The frontend always
// connects to the same-origin /mqtt proxy (see _mqttDefault), so the broker's
// address never belongs in the browser. Caching it here was the source of the
// stale-port "Demo fallback" bug. A manual override is still available via the
// Settings → MQTT field (wactorz-mqtt-url), which this never touches.
fetch(`${_apiBase}/api/config`)
    .then(r => (r.ok ? r.json() : null))
    .then(cfg => {
        if (!cfg) {
            return;
        }
        // Seed `key` from the server value, letting .env changes propagate while
        // preserving deliberate local edits.
        const seedFromServer = (key: string, value: string) => {
            if (!value) {
                return;
            }
            const baselineKey = `${key}__server`;
            const lastServer = localStorage.getItem(baselineKey);
            // Server value changed (or never seeded) → adopt it as the active value.
            if (value !== lastServer) {
                localStorage.setItem(key, value);
                localStorage.setItem(baselineKey, value);
            }
        };
        seedFromServer("wactorz-ha-url", cfg.ha?.url ?? "");
        seedFromServer("wactorz-ha-token", cfg.ha?.token ?? "");
    })
    .catch(err => log.debug("[config] /api/config seed failed:", err));

function pushFeed(item: FeedItem): void {
    emit("af-feed-push", { item });
}

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

// Seed only once — MQTT reconnects must not re-add already-known agents.
let seeded = false;

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

// HA entity state-changes arrive over two transports; a single pusher filters
// and de-duplicates them. See ui/haFeed.
const pushHaFeed = createHaFeedPusher(pushFeed);

// Path 1: direct HA WebSocket via HAClient (always works when HA is configured in frontend)
listen("af-ha-state-change", detail => {
    const { entityId, state, friendlyName } = detail;
    pushHaFeed(entityId, state, friendlyName);
});

// Path 2: ha-state-bridge-agent → MQTT ha/state/{domain}/{entity_id}
mqtt.on("raw", ({ topic, payload }) => {
    const ev = parseHaRawEvent(topic, payload);
    if (ev) {
        pushHaFeed(ev.entityId, ev.state, ev.friendlyName);
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

// Probe server TTS availability + load voice list (base must be set first so
// the request stays inside the ingress prefix instead of bare "/api").
tts.setApiBase(_apiBase);
tts.init();

mqtt.connect();

window.addEventListener("beforeunload", () => {
    mqtt.disconnect();
    wsChat.disconnect();
    agentStore.dispose();
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
