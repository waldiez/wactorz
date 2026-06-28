/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wactorz Dashboard — entry point.
 *
 * Bootstrap order:
 * 1. Create SceneManager (agent-state store + CardDashboard coordinator)
 * 2. Create MQTTClient and connect to broker
 * 3. Wire MQTT events → SceneManager (which drives the cards dashboard)
 *
 * The chat lives entirely in CardDashboard's in-card bar (DashboardChat),
 * which renders from the af-chat-message / af-stream-* events IOManager emits.
 */

import "./app.css";
import { SceneManager } from "./scene/SceneManager";
import { MQTTClient } from "./mqtt/MQTTClient";
import { IOManager } from "./io/IOManager";
import { WSChatClient } from "./io/WSChatClient";
import { tts } from "./io/TTSManager";
import { desktopNotifyBackground, clearUnreadBadge, initNotifications } from "./io/DesktopNotify";
import { toast } from "./ui/ToastManager";
import { createHaFeedPusher } from "./ui/haFeed";
import { DropZone } from "./ui/DropZone";
import { UPLOADS_ENABLED } from "./ui/dashboard/uploads";
import type { AgentInfo } from "./types/agent";
import type { FeedItem } from "./types/feed";
import { resolveAgentName } from "./agents/naming";
import { toAgentInfo, mapLogFeedItem, buildNameIndex } from "./agents/mapping";
import { createDeletionGuard } from "./agents/deletionGuard";

const scene = new SceneManager();

// Cards is the only view; clear any stale persisted theme from older builds.
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

initNotifications();

const _ingressPath: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";

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

// Self-heal browsers that cached a URL under older builds (incl. the bad :8888
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
            scene.reconcileAgents(
                actors
                    .filter(a => !isDeleted(a.id))
                    .map(a => ({
                        ...a,
                        name: resolveAgentName(a.name, a.id),
                    })),
            );
            console.info(`[Dashboard] reconciled ${actors.length} live actors from REST`);
        })
        .catch(() => {
            // Dev mode without a running server — ignore silently.
        })
        .finally(() => {
            liveSyncInFlight = false;
        });
}

// Non-streaming replies (slash commands, errors, one-shot agent replies)
wsChat.onChat((content, from, timestampMs) => {
    toast.show({ type: "chat", title: from, message: content.slice(0, 120) });
    desktopNotifyBackground(from, content.slice(0, 120));
    const msg = {
        id: `ws-${timestampMs}`,
        from,
        to: "user",
        content,
        timestampMs,
    };
    ioManager.receiveAgentMessage(msg);
    scene.onChat(from, "user");
    const feedItem = {
        type: "chat" as const,
        label: content,
        agentName: from,
        timestamp: timestampMs,
    };
    document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item: feedItem } }));
    document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
});

// Streaming replies — onStreamChunk / onStreamEnd are wired inside setWSClient
ioManager.setWSClient(wsChat);

// State patches broadcast by the server over the same /ws connection.
// This is how pause/stop/resume state changes reach the UI without polling.
wsChat.onStatePatch((agents, deletedId, stats) => {
    if (deletedId) {
        markDeleted(deletedId);
        scene.removeAgent(deletedId);
    }
    if (stats?.totalCostUsd !== undefined) {
        scene.setTotalCostUsd(stats.totalCostUsd);
    }
    if (stats?.totalMessages !== undefined) {
        scene.setTotalMessages(stats.totalMessages);
    }
    agents.forEach(a => {
        if (!a.agent_id || isDeleted(a.agent_id)) {
            return;
        }
        scene.addOrUpdateAgent(toAgentInfo(a));
    });
});

wsChat.connect(`${_wsBase}/ws`);
refreshLiveActors();
window.setInterval(() => {
    refreshLiveActors();
    scene.pruneStaleRemoteAgents();
}, 15000);

// The server embeds its in-memory log_feed (spawned/status/logs/alerts) in
// every state-patch.  We use this as a reliable secondary path so MQTT events
// appear in the feed even when the direct Mosquitto WebSocket is unavailable.

let _logFeedMaxTs = 0;
let _logFeedInitialized = false;
let _mqttLive = false;

wsChat.onLogFeed(items => {
    // Nameless entries (e.g. `log`) borrow their friendly name from the
    // `spawned` entry in the same batch, then from the live scene — so reloads
    // attribute them by name instead of a raw id.
    const nameIndex = buildNameIndex(items);
    const resolveName = (id: string): string | undefined =>
        nameIndex.get(id) ?? scene.getAgents().find(a => a.id === id)?.name;

    if (!_logFeedInitialized) {
        _logFeedInitialized = true;
        _logFeedMaxTs = items.length ? Math.max(...items.map(i => i.timestamp ?? 0)) : 0;
        // Push historical items (happened before browser connected — MQTT won't re-deliver them).
        [...items].reverse().forEach(item => {
            const mapped = mapLogFeedItem(item, resolveName);
            if (mapped) {
                pushFeed(mapped);
            }
        });
        return;
    }

    // Always advance the high-water mark so that when MQTT reconnects and later
    // disconnects again, we don't replay the entire backlog.
    const newItems = items.filter(item => (item.timestamp ?? 0) > _logFeedMaxTs);
    if (newItems.length) {
        _logFeedMaxTs = Math.max(...newItems.map(i => i.timestamp ?? 0));
    }

    // Direct MQTT is live — it delivers these events already; skip to avoid duplicates.
    if (_mqttLive) {
        return;
    }

    // Push new items oldest-first so the feed stays chronological.
    [...newItems].reverse().forEach(item => {
        const mapped = mapLogFeedItem(item, resolveName);
        if (mapped) {
            pushFeed(mapped);
        }
    });
});

// Seed the activity feed from SQLite chat_log so the feed panel isn't empty
// after a server restart. The server returns real Unix timestamps (seconds);
// convert to ms for the feed.
fetch(`${_apiBase}/api/feed`)
    .then(r => (r.ok ? r.json() : []))
    .then(
        (items: { type: string; label: string; agentName: string; timestamp?: number; role?: string }[]) => {
            console.log("[feed] /api/feed seed:", items.length, "items");
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
    .catch(() => {});

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
    .catch(() => {});

function pushFeed(item: FeedItem): void {
    document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item } }));
}

mqtt.on("heartbeat", payload => {
    if (isDeleted(payload.agentId, payload.timestampMs)) {
        return;
    }
    scene.onHeartbeat(payload);
    pushFeed({
        type: "heartbeat",
        label: "heartbeat",
        agentName: payload.agentName,
        timestamp: payload.timestampMs,
    });
});

mqtt.on("spawn", payload => {
    // Block a stale spawn from the stop-window; a respawn carries a newer
    // timestamp and is re-admitted by isDeleted().
    if (isDeleted(payload.agentId, payload.timestampMs)) {
        return;
    }
    scene.onSpawn(payload);
    pushFeed({
        type: "spawn",
        label: `spawned (${payload.agentType ?? "agent"})`,
        agentName: payload.agentName,
        timestamp: payload.timestampMs,
    });
    toast.show({
        type: "spawn",
        title: payload.agentName,
        message: `${payload.agentType ?? "agent"} is online`,
    });
    desktopNotifyBackground("Agent spawned", `${payload.agentName} is online`);
});

mqtt.on("alert", payload => {
    scene.onAlert(payload);
    const alertMsg = payload.message ?? "";
    const alertAgent = payload.agentName ?? "system";
    pushFeed({
        type: payload.severity === "error" ? "alert-error" : "alert-warning",
        label: alertMsg,
        agentName: alertAgent,
        timestamp: payload.timestampMs,
    });
    const isError = payload.severity === "error" || payload.severity === "critical";
    toast.show({
        type: isError ? "alert-error" : "alert-warning",
        title: alertAgent,
        message: alertMsg.slice(0, 120),
    });
    desktopNotifyBackground(isError ? `⚠ ${alertAgent}` : alertAgent, alertMsg.slice(0, 100));
});

mqtt.on("chat", msg => {
    if (msg.from !== "user") {
        toast.show({ type: "chat", title: msg.from, message: msg.content.slice(0, 120) });
        desktopNotifyBackground(msg.from, msg.content.slice(0, 120));
    }
    ioManager.receiveAgentMessage(msg);
    scene.onChat(msg.from, msg.to);
    document.dispatchEvent(new CustomEvent("af-chat-message", { detail: { msg } }));
    pushFeed({
        type: "chat",
        label: `→ ${msg.to}: ${msg.content}`,
        agentName: msg.from,
        timestamp: msg.timestampMs,
    });
});

mqtt.on("status", payload => {
    if (!isDeleted(payload.agentId)) {
        scene.addOrUpdateAgent({
            id: payload.agentId,
            name: payload.agentName,
            state: payload.state,
            protected: payload.protected ?? false,
            messagesProcessed: payload.messagesProcessed,
        });
    }
    if (payload.state === "stopped") {
        window.setTimeout(() => refreshLiveActors(), 200);
        pushFeed({
            type: "stopped",
            label: "stopped",
            agentName: payload.agentName,
            timestamp: Date.now(),
        });
    }
});

// Seed only once — MQTT reconnects must not re-add already-known agents.
let seeded = false;

mqtt.on("connected", () => {
    _mqttLive = true;
    console.info("[Dashboard] MQTT connected");
    document.dispatchEvent(new CustomEvent("af-connection-status", { detail: { status: "live" } }));

    scene.pruneStaleRemoteAgents();

    if (seeded) {
        return;
    }
    seeded = true;

    // Startup spawn events are published before the browser connects.
    // Fetch the current actor list from REST so they appear immediately.
    refreshLiveActors();
});

mqtt.on("qa-flag", payload => {
    pushFeed({
        type: "qa-flag",
        label: `[${payload.category}] ${payload.excerpt}`,
        agentName: `qa-agent ← ${payload.from}`,
        timestamp: payload.timestampMs,
    });
});

mqtt.on("metrics", payload => {
    // Merge cost/message metrics into the agent record so dashboards can display them.
    const existing = scene.getAgents().find(a => a.id === payload.agentId);
    if (!existing) {
        return;
    }
    const update: AgentInfo = {
        id: payload.agentId,
        name: existing.name,
        state: existing.state,
        protected: existing.protected,
    };
    if (payload.messagesProcessed !== undefined) {
        update.messagesProcessed = payload.messagesProcessed;
    }
    if (payload.costUsd !== undefined) {
        update.costUsd = payload.costUsd;
    }
    if (payload.uptime !== undefined) {
        update.uptime = payload.uptime;
    }
    scene.addOrUpdateAgent(update);
});

mqtt.on("logs", payload => {
    const msg = payload.message ?? payload.text ?? "";
    if (!msg) {
        return;
    }
    pushFeed({
        type: "chat",
        label: msg,
        agentName: payload.agentName,
        timestamp: Date.now(),
    });
});

mqtt.on("completed", payload => {
    pushFeed({
        type: "spawn",
        label: "task completed",
        agentName: payload.agentName,
        timestamp: Date.now(),
    });
});

mqtt.on("node-heartbeat", payload => {
    scene.updateRemoteNode(payload.node, payload.agents);
    pushFeed({
        type: "health",
        label: `node online · ${payload.agents.length} agent${payload.agents.length !== 1 ? "s" : ""}`,
        agentName: payload.node,
        timestamp: Date.now(),
    });
});

mqtt.on("host-stats", stats => {
    if (stats.cpu !== undefined || stats.memUsedMb !== undefined) {
        scene.setHostStats(stats.cpu ?? 0, stats.memUsedMb ?? 0, stats.memTotalMb);
    }
});

mqtt.on("coin", payload => {
    pushFeed({
        type: "qa-flag",
        label: `balance ${payload.balance}${payload.reason ? " · " + payload.reason : ""}`,
        agentName: "wiz-agent",
        timestamp: Date.now(),
    });
});

// HA entity state-changes arrive over two transports; a single pusher filters
// and de-duplicates them. See ui/haFeed.
const pushHaFeed = createHaFeedPusher(pushFeed);

// Path 1: direct HA WebSocket via HAClient (always works when HA is configured in frontend)
document.addEventListener("af-ha-state-change", e => {
    const { entityId, state, friendlyName } = (
        e as CustomEvent<{ entityId: string; state: string; friendlyName: string }>
    ).detail;
    pushHaFeed(entityId, state, friendlyName);
});

// Path 2: ha-state-bridge-agent → MQTT ha/state/{domain}/{entity_id}
mqtt.on("raw", ({ topic, payload }) => {
    if (!topic.startsWith("ha/")) {
        return;
    }
    const p = payload as Record<string, unknown>;
    const entityId = (p["entity_id"] as string | undefined) ?? topic.split("/").slice(-2).join(".");
    const newState = p["new_state"] as Record<string, unknown> | undefined;
    const state = (newState?.["state"] as string | undefined) ?? "";
    const attrs = newState?.["attributes"] as Record<string, unknown> | undefined;
    const friendlyName = (attrs?.["friendly_name"] as string | undefined) ?? entityId;
    if (!state) {
        return;
    }
    pushHaFeed(entityId, state, friendlyName);
});

mqtt.on("disconnected", () => {
    _mqttLive = false;
    console.warn("[Dashboard] MQTT disconnected");
    document.dispatchEvent(new CustomEvent("af-connection-status", { detail: { status: "demo" } }));
});

mqtt.on("error", err => {
    console.error("[Dashboard] MQTT error:", err);
});

// Streaming reply finished — notify
document.addEventListener("af-stream-end", e => {
    const { text, from } = (e as CustomEvent<{ text: string | null; from: string }>).detail;
    if (!text) {
        return;
    }
    toast.show({ type: "chat", title: from, message: text.slice(0, 120) });
    desktopNotifyBackground(from, text.slice(0, 120));
});

// Agent commands from CardDashboard → WebSocket
document.addEventListener("af-agent-command", e => {
    const { command, agentId } = (e as CustomEvent<{ command: string; agentId: string }>).detail;
    if (command === "delete") {
        // Mark deleted immediately so MQTT "stopped" events don't re-add the card
        // before the WS state-patch reply arrives.
        markDeleted(agentId);
        scene.removeAgent(agentId);
    }
    wsChat.sendRaw({ type: "command", command, agent_id: agentId });
});

// af-iobar sends: route through ioManager (same as regular io-bar)
document.addEventListener("af-send-message", e => {
    const { content } = (e as CustomEvent<{ content: string; target: string }>).detail;
    const agent =
        scene.getAgents().find(a => a.name === (e as CustomEvent<{ target: string }>).detail.target) ?? null;
    void ioManager.send(content, agent);
});

// Probe server TTS availability + load voice list (base must be set first so
// the request stays inside the ingress prefix instead of bare "/api").
tts.setApiBase(_apiBase);
tts.init();

mqtt.connect();

window.addEventListener("focus", () => clearUnreadBadge());

window.addEventListener("beforeunload", () => {
    mqtt.disconnect();
    wsChat.disconnect();
    scene.dispose();
});

// wipe all
document.addEventListener("af-wipe-all", () => {
    scene.clearAll();
    _logFeedMaxTs = 0;
});

// A scoped reset (metrics / logs) cleared the server-side activity log — drop
// the on-screen feed too, since onLogFeed only ever appends and would otherwise
// keep showing stale lines until the next event.
document.addEventListener("af-clear-feed", () => {
    _logFeedMaxTs = 0;
});
