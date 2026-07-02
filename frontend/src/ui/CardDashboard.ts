/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * CardDashboard — Wactorz.
 *
 * Full-screen overlay with af-header + af-body + af-iobar layout.
 * Views: overview (stats + cards + nodes) | feed | chat (embedded).
 *
 * Connects to the rest of the app via document-level custom events:
 *   Listens: "af-feed-push"  { item: FeedItem }
 *            "af-chat-message" { msg: ChatMessage }
 *            "af-stream-chunk" { chunk, from }
 *            "af-stream-end"
 *            "af-connection-status" { status: "live"|"connecting"|"demo" }
 *   Fires:   "af-send-message" { content, target }
 */

import type { AgentInfo } from "../types/agent";
import { safeStorage } from "../safeStorage";
import type { FeedItem } from "../types/feed";
import { buildHeader, buildBottomNav, setHaNavUrl } from "./dashboard/header";
import { stateLabel, relTime, sortAgents, STALE_MS } from "./dashboard/agentState";
import type { View, ConnState } from "./dashboard/types";
import { buildFeedView, appendFeedItemToView, feedKey } from "./dashboard/feedView";
import { DashboardChat } from "./dashboard/DashboardChat";
import { OverviewView } from "./dashboard/overview";
import { MetricsController } from "./dashboard/metrics";
import { seedHaConfigFromServer } from "./dashboard/haConfig";
import { emit, listen } from "../events";

export class CardDashboard {
    private root: HTMLElement;
    private agents: Map<string, AgentInfo> = new Map();
    private lastHb: Map<string, number> = new Map();
    private feedItems: FeedItem[] = [];
    /** Content keys of items in `feedItems`, for O(1) dedup across feed sources. */
    private _feedKeys = new Set<string>();
    /** The chat concern (thread, iobar, streaming, history) lives in its own controller. */
    private _chat: DashboardChat;
    /** The overview view (host bar, stat cards, wactor grid, nodes panel). */
    private _overview: OverviewView;
    private view: View = "overview";
    private connState: ConnState = "connecting";
    private tickTimer: ReturnType<typeof setInterval> | null = null;
    private hideHeartbeats: boolean = true;

    private _remoteNodes = new Map<string, { agents: string[]; lastSeen: number }>();
    private _removingIds = new Set<string>();

    /** Cost / message totals + host-resource telemetry live in their own controller. */
    private _metrics: MetricsController;

    // Event listeners (stored for cleanup)
    private _evFeed: ((e: Event) => void) | null = null;
    private _evConn: ((e: Event) => void) | null = null;
    private _wipeAll: ((e: Event) => void) | null = null;
    private _clearFeed: ((e: Event) => void) | null = null;

    /** HA base URL (seeded from /api/config) — the Devices nav button links to it. */
    private get haUrl(): string | null {
        return safeStorage.get("wactorz-ha-url") || null;
    }

    constructor() {
        this.root = this.buildRoot();
        this._chat = new DashboardChat({
            root: this.root,
            agents: this.agents,
            getView: () => this.view,
            setView: v => this._setView(v),
            sortedAgents: () => sortAgents(this.agents.values()),
        });
        this._metrics = new MetricsController({
            root: this.root,
            getView: () => this.view,
            renderStats: () => this._overview.renderStats(),
            renderView: () => this._renderView(),
        });
        this._overview = new OverviewView({
            root: this.root,
            agents: this.agents,
            lastHb: this.lastHb,
            remoteNodes: this._remoteNodes,
            removingIds: this._removingIds,
            hostStats: () => this._metrics.hostStats(),
            statData: () => ({
                agents: [...this.agents.values()],
                totalMessages: this._metrics.totalMessages,
                totalCostUsd: this._metrics.totalCostUsd,
                feedCount: this.feedItems.length,
                costLimit: this._metrics.costLimitInfo,
            }),
            onChat: name => {
                this._chat.setTarget(name);
                this._setView("chat");
            },
            onCommand: (id, action, btn) => this._sendCommand(id, action, btn),
        });
        this.root.appendChild(this._chat.buildIobar());
        document.body.appendChild(this.root);
        void this._loadServerConfig();
    }

    /** Seed the HA URL from /api/config and point the Devices nav link at it. */
    private async _loadServerConfig(): Promise<void> {
        if (!(await seedHaConfigFromServer())) {
            return;
        }
        setHaNavUrl(this.root, this.haUrl);
    }

    /** Reveal the dashboard, seed it with `agents`, wire events, and start the refresh timers. */
    show(agents: AgentInfo[]): void {
        agents.forEach(a => this.agents.set(a.id, a));
        this.root.classList.add("cd-visible");
        this._wireEvents();
        this._renderView();
        this.tickTimer = setInterval(() => this._refreshTimestamps(), 5000);
        this._metrics.startPolling();
    }

    /** Hide the dashboard, unwire events, release the mic, and stop timers. */
    hide(): void {
        this.root.classList.remove("cd-visible");
        this._unwireEvents();
        this._chat.cancelMic(); // release the mic if a recording was in progress
        if (this.tickTimer) {
            clearInterval(this.tickTimer);
            this.tickTimer = null;
        }
        this._metrics.stopPolling();
    }

    /** Hide and remove the dashboard from the DOM. */
    destroy(): void {
        this.hide();
        this.root.remove();
    }

    /** Set the aggregate cost figure. */
    setTotalCostUsd(usd: number): void {
        this._metrics.setTotalCostUsd(usd);
    }

    /** Set the aggregate message count. */
    setTotalMessages(count: number): void {
        this._metrics.setTotalMessages(count);
    }

    /** Set host CPU/memory telemetry. */
    setHostStats(cpu: number, memUsedMb: number, memTotalMb?: number): void {
        this._metrics.setHostStats(cpu, memUsedMb, memTotalMb);
    }

    /** Add an agent and refresh the affected views. */
    addAgent(agent: AgentInfo): void {
        this.agents.set(agent.id, agent);
        if (!this.root.classList.contains("cd-visible")) {
            return;
        }
        if (this.view === "overview") {
            this._overview.renderCards();
            this._overview.renderStats();
            this._overview.renderNodes();
        }
        if (this.view === "chat") {
            this._chat.renderSidebar();
        }
        this._chat.updateTargetSelect();
    }

    /** Update an agent in place and refresh the affected views. */
    updateAgent(agent: AgentInfo): void {
        this.agents.set(agent.id, agent);
        if (!this.root.classList.contains("cd-visible")) {
            return;
        }
        this._overview.patchCard(agent);
        if (this.view === "overview") {
            this._overview.renderStats();
        }
        if (this.view === "chat") {
            this._chat.renderSidebar();
        }
    }

    /** Remove an agent (with exit animation) and forget its chat history. */
    removeAgent(id: string): void {
        const removed = this.agents.get(id);
        this.agents.delete(id);
        this.lastHb.delete(id); // else churned agents leak dead entries _refreshTimestamps scans
        // history is keyed by agent NAME, not UUID — look up name before deleting
        if (removed) {
            this._chat.forgetHistory(removed.name);
        }
        if (!this.root.classList.contains("cd-visible")) {
            return;
        }
        const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
        if (card) {
            this._removingIds.add(id);
            card.style.animation = "cd-exit 0.25s ease forwards";
            setTimeout(() => {
                card.remove();
                this._removingIds.delete(id);
            }, 250);
        }
        if (this.view === "overview") {
            this._overview.renderStats();
            this._overview.renderNodes();
        }
        if (this.view === "chat") {
            this._chat.renderSidebar();
        }
        this._chat.updateTargetSelect();
    }

    /** Record a remote node's agent list and refresh the nodes panel. */
    updateRemoteNode(name: string, agents: string[]): void {
        this._remoteNodes.set(name, { agents, lastSeen: Date.now() });
        if (this.view === "overview") {
            this._overview.renderNodes();
        }
    }

    /** Record a heartbeat timestamp and pulse the agent's card. */
    onHeartbeat(agentId: string, timestampMs: number, _cpu?: number, _mem?: number): void {
        this.lastHb.set(agentId, timestampMs);
        if (!this.root.classList.contains("cd-visible")) {
            return;
        }
        if (this._removingIds.has(agentId)) {
            return;
        }
        const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
        if (!card) {
            return;
        }
        const hbEl = card.querySelector<HTMLElement>(".af-card-hb-time");
        if (hbEl) {
            hbEl.textContent = relTime(timestampMs);
        }
        const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
        if (dot) {
            dot.classList.remove("af-card-pulse", "af-card-stale");
            void dot.offsetWidth;
            dot.classList.add("af-card-pulse");
        }
    }

    /** Briefly flash an alert class (error/warning) on the agent's card. */
    showAlert(agentId: string, severity: string): void {
        const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
        if (!card) {
            return;
        }
        const cls =
            severity === "error" || severity === "critical" ? "af-card-alert-error" : "af-card-alert-warn";
        card.classList.add(cls);
        setTimeout(() => card.classList.remove(cls, "af-card-alert-error", "af-card-alert-warn"), 900);
    }

    /** Briefly flash the sender's card to signal chat activity. */
    onChat(fromId: string, _toId: string): void {
        const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
        if (!card) {
            return;
        }
        card.classList.add("af-card-chat-flash");
        setTimeout(() => card.classList.remove("af-card-chat-flash"), 600);
    }

    private _wireEvents(): void {
        this._wireFeedEvents();
        this._chat.wire();
        this._evConn = listen("af-connection-status", detail => {
            this.connState = detail.status;
            this._renderConnBadge();
            this._renderHealth();
        });
    }

    private _wireFeedEvents(): void {
        this._evFeed = listen("af-feed-push", detail => {
            const item = detail.item;
            // The same event can arrive from several sources (SQLite seed, WS
            // log_feed replay, live MQTT/chat); drop exact duplicates so the feed
            // doesn't double up or render out of order on rebuild.
            const key = feedKey(item);
            if (this._feedKeys.has(key)) {
                return;
            }
            this._feedKeys.add(key);
            this.feedItems.push(item);
            if (this.feedItems.length > 500) {
                const dropped = this.feedItems.shift();
                if (dropped) {
                    this._feedKeys.delete(feedKey(dropped));
                }
            }
            if (this.view === "feed") {
                this._appendFeedItemToView(item);
            }
        });

        this._wipeAll = listen("af-wipe-all", () => {
            this.feedItems = [];
            this._feedKeys.clear();
            this._chat.clearAll();
            this._renderView();
        });

        // A metrics/logs reset cleared the server-side activity log — drop this
        // dashboard's own in-card feed too.
        this._clearFeed = listen("af-clear-feed", () => {
            this.feedItems = [];
            this._feedKeys.clear();
            this._renderView();
        });
    }

    private _unwireEvents(): void {
        this._chat.unwire();
        const pairs: [string, EventListener | null][] = [
            ["af-feed-push", this._evFeed],
            ["af-connection-status", this._evConn],
            ["af-wipe-all", this._wipeAll],
            ["af-clear-feed", this._clearFeed],
        ];
        pairs.forEach(([name, fn]) => {
            if (fn) {
                document.removeEventListener(name, fn);
            }
        });
        this._evFeed = this._evConn = this._wipeAll = this._clearFeed = null;
    }

    private _renderView(): void {
        const body = this.root.querySelector<HTMLElement>(".af-body")!;
        body.innerHTML = "";
        this._mountActiveView(body);
        this._syncViewChrome();
    }

    /** Build and attach the element for the current view. */
    private _mountActiveView(body: HTMLElement): void {
        if (this.view === "overview") {
            body.appendChild(this._overview.build());
        } else if (this.view === "feed") {
            body.appendChild(this._buildFeedView());
        } else if (this.view === "settings") {
            body.appendChild(this._buildSettingsView());
        } else if (this.view === "chat") {
            body.appendChild(this._chat.buildChatView());
            // buildChatView()'s renders run before the element is in the DOM
            // (querySelector returns null); re-run + load history once attached.
            this._chat.afterMount();
        }
    }

    /** Sync the header view buttons, health line and target-select to the view. */
    private _syncViewChrome(): void {
        this.root.querySelectorAll<HTMLElement>(".af-view-btn[data-view]").forEach(btn => {
            btn.classList.toggle("active", btn.dataset["view"] === this.view);
        });
        this._renderHealth();
        // Only show the agent-target dropdown in the chat view
        const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
        if (select) {
            select.style.display = this.view === "chat" ? "" : "none";
            if (this.view === "chat") {
                select.value = this._chat.chatTarget;
            }
        }
    }

    private _setView(v: View): void {
        if (v === "chat") {
            this._chat.syncChatTarget();
        }
        this.view = v;
        this._renderView();
    }

    private _buildFeedView(): HTMLElement {
        return buildFeedView(this.feedItems, {
            hideHeartbeats: this.hideHeartbeats,
            onToggleHeartbeats: h => {
                this.hideHeartbeats = h;
            },
        });
    }

    private _appendFeedItemToView(item: FeedItem): void {
        appendFeedItemToView(this.root, item, this.hideHeartbeats);
    }

    private _renderConnBadge(): void {
        const badge = this.root.querySelector<HTMLElement>(".af-conn-badge");
        if (!badge) {
            return;
        }
        badge.className = `af-conn-badge af-conn-${this.connState}`;
        badge.textContent =
            this.connState === "live"
                ? "● live"
                : this.connState === "connecting"
                  ? "○ Connecting…"
                  : "◎ Demo fallback";
    }

    private _renderHealth(): void {
        const el = this.root.querySelector<HTMLElement>(".af-health");
        if (!el) {
            return;
        }
        const agents = [...this.agents.values()];
        const healthy = agents.filter(a => stateLabel(a.state) === "running").length;
        el.textContent = `${healthy}/${agents.length} wactorz healthy`;
    }

    private _sendCommand(
        id: string,
        action: "pause" | "resume" | "stop" | "delete",
        btn?: HTMLButtonElement,
    ): void {
        if (btn) {
            btn.disabled = true;
            btn.classList.add("sending");
            setTimeout(() => {
                btn.disabled = false;
                btn.classList.remove("sending");
            }, 600);
        }
        emit("af-agent-command", { command: action, agentId: id });
    }

    private _refreshTimestamps(): void {
        const now = Date.now();
        this.lastHb.forEach((ms, id) => {
            const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
            if (!card) {
                return;
            }
            const el = card.querySelector<HTMLElement>(".af-card-hb-time");
            if (el) {
                el.textContent = relTime(ms);
            }
            const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
            if (dot) {
                dot.classList.toggle("af-card-stale", now - ms > STALE_MS);
            }
        });
    }

    private buildRoot(): HTMLElement {
        const root = document.createElement("div");
        root.id = "card-dashboard";
        root.className = "cd-root";

        const body = document.createElement("div");
        body.className = "af-body";

        // The iobar is owned by the chat controller and appended in the constructor.
        const onSetView = (v: View) => this._setView(v);
        const haUrl = this.haUrl;
        root.append(
            buildHeader({ view: this.view, connState: this.connState, onSetView, haUrl }),
            body,
            buildBottomNav({ view: this.view, onSetView, haUrl }),
        );
        return root;
    }

    private _buildSettingsView(): HTMLElement {
        return this._metrics.buildSettingsView();
    }
}
