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
 * Draws the agents and nodes the `AgentStore` holds, and keeps none of its
 * own: it subscribes, and redraws what each batch of changes touched.
 *
 * Connects to the rest of the app through document-level `af-*` events, typed
 * in `events.ts`. This class handles the ones about the dashboard as a whole —
 * feed pushes, connection state, resets, an extension's view; the chat and
 * application-log events belong to `DashboardChat` and `AppLogFeed`, which wire
 * their own.
 */

import type { AgentInfo } from "../types/agent";
import type { AgentActivity, AgentChanges, AgentStore } from "../agents/AgentStore";
import { safeStorage } from "../safeStorage";
import {
    buildHeader,
    buildBottomNav,
    setHaNavUrl,
    releaseHeaderPopovers,
    releaseBottomNav,
    renderConnBadge,
} from "./dashboard/header";
import { setSignOutVisible } from "./dashboard/signOut";
import { stateLabel, sortAgents } from "./dashboard/agentState";
import type { View, ConnState } from "./dashboard/types";
import { IconName } from "./dashboard/icons";
import { ActivityFeed } from "./dashboard/activityFeed";
import { Heartbeats } from "./dashboard/heartbeats";
import { DashboardChat } from "./dashboard/DashboardChat";
import { OverviewView } from "./dashboard/overview";
import { OverviewData } from "./dashboard/overviewData";
import { TrendPanel } from "./dashboard/trendPanel";
import type { AgentAction } from "./dashboard/cards";
import { el, testId } from "./dom";
import { MetricsController } from "./dashboard/metrics";
import { confirmDialog } from "./dashboard/confirmDialog";
import { seedServerConfig } from "../config/serverConfig";
import { emit, Listeners } from "../events";
import { initialRoute } from "../router";
import { ViewRoutes } from "./dashboard/viewRoutes";
import { ModelsView } from "./models/ModelsView";
import { ModelCatalog } from "../models/ModelCatalog";
import { ModelsApi } from "../io/modelsApi";

export class CardDashboard {
    private root: HTMLElement;
    /** Last-heard-from times, and the card bits that show them. */
    private _heartbeats: Heartbeats;
    /** The activity view's model — agent events and application-log records. */
    private _activity: ActivityFeed;
    /** The chat concern (thread, iobar, streaming, history) lives in its own controller. */
    private _chat: DashboardChat;
    /** The overview view (host bar, stat cards, wactor grid, nodes panel). */
    private _overview: OverviewView;
    private view: View = "overview";
    private connState: ConnState = "connecting";
    private tickTimer: ReturnType<typeof setInterval> | null = null;
    /** Stops the store telling this dashboard of changes. */
    private readonly _unsubscribe: () => void;
    /** The trends and node manifests the overview fetches on a timer. */
    private _data: OverviewData;
    /** An agent's or a node's history, opened from its card. */
    private _trendPanel = new TrendPanel();
    /** The stored models and their pointers. */
    private _models = new ModelsView(new ModelCatalog(new ModelsApi(window.__WACTORZ_INGRESS_PATH ?? "")));
    private _removingIds = new Set<string>();

    /** Cost / message totals + host-resource telemetry live in their own controller. */
    private _metrics: MetricsController;

    /** Extension-registered view builders (name → element factory). */
    private _viewBuilders = new Map<string, () => HTMLElement>();
    /** Extra nav buttons registered by extensions. */
    private _extraViews: { key: string; label: string; icon: IconName }[] = [];

    /** What `_wireEvents` subscribed to, dropped together on teardown. */
    private _listeners = new Listeners();
    /** Opening what the address names, and saying where the dashboard is. */
    private _routes: ViewRoutes;

    /** HA base URL (seeded from /api/config) — the Devices nav button links to it. */
    private get haUrl(): string | null {
        return safeStorage.get("wactorz-ha-url") || null;
    }

    constructor(private readonly store: AgentStore) {
        this.root = this.buildRoot();
        this._heartbeats = new Heartbeats(this.root);
        this._chat = new DashboardChat({
            root: this.root,
            agents: store.agents,
            getView: () => this.view,
            setView: v => this._setView(v),
            sortedAgents: () => sortAgents(store.agents.values()),
            label: agent => store.label(agent),
        });
        this._metrics = new MetricsController({
            root: this.root,
            getView: () => this.view,
            renderStats: () => this._overview.renderStats(),
            renderView: () => this._renderView(),
        });
        this._routes = new ViewRoutes({
            view: () => this.view,
            setView: v => this._setView(v),
            render: () => this._renderView(),
            // The address names the conversation by its agent's name, or `name@node` where it is shared.
            chatTarget: () => this._chat.chatTargetAddress,
            setChatTarget: name => this._chat.openByName(name),
            model: () => this._models.open,
            setModel: name => this._models.show(name),
        });
        this._overview = new OverviewView({
            root: this.root,
            agents: store.agents,
            lastHb: this._heartbeats.lastSeen,
            remoteNodes: store.nodes,
            removingIds: this._removingIds,
            label: agent => store.label(agent),
            hostStats: () => this._metrics.hostStats(),
            statData: () => ({
                agents: store.list(),
                totalMessages: this._metrics.totalMessages,
                totalCostUsd: this._metrics.totalCostUsd,
                feedCount: this._activity.count,
                costLimit: this._metrics.costLimitInfo,
            }),
            onChat: id => {
                this._chat.setTarget(id);
                this._setView("chat");
            },
            onCommand: (id, action, btn) => this._sendCommand(id, action, btn),
            agentTrend: name => this._data.agentRates.get(name),
            nodeTrend: name => this._data.nodeTrends.get(name),
            onOpenTrend: (kind, name) => void this._trendPanel.open(kind, name),
        });
        this._data = new OverviewData({
            isOverview: () => this.view === "overview",
            nodeNames: () => [...store.nodes.keys()],
            onUpdate: () => this._onDataUpdate(),
        });
        this._activity = new ActivityFeed({
            root: this.root,
            isFeedView: () => this.view === "feed",
        });
        this.root.appendChild(this._chat.buildIobar());
        document.body.appendChild(this.root);
        this._unsubscribe = store.subscribe(changes => this._onAgentsChanged(changes));
        void this._loadServerConfig();
    }

    /** Reveal the dashboard, wire events, and start the refresh timers. */
    show(): void {
        this.root.classList.add("cd-visible");
        this._wireEvents();
        this._routes.open(initialRoute());
        this.tickTimer = setInterval(() => {
            if (!document.hidden) {
                this._heartbeats.refresh();
            }
        }, 5000);
        this._metrics.startPolling();
        this._data.start();
    }

    /** Hide the dashboard, unwire events, release the mic, and stop timers. */
    hide(): void {
        this.root.classList.remove("cd-visible");
        this._unwireEvents();
        this._chat.cancelMic(); // release the mic if a recording was in progress
        this._activity.release();
        if (this.tickTimer) {
            clearInterval(this.tickTimer);
            this.tickTimer = null;
        }
        this._metrics.stopPolling();
        this._data.stop();
        this._trendPanel.close();
    }

    /** Hide and remove the dashboard from the DOM. */
    destroy(): void {
        this.hide();
        this._unsubscribe();
        this._trendPanel.destroy();
        this._models.destroy();
        releaseHeaderPopovers();
        releaseBottomNav();
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

    /** Draw what a batch of the store's changes did. */
    private _onAgentsChanged(changes: AgentChanges): void {
        changes.removed.forEach((agent, id) => this._forget(id, agent));
        const visible = this.root.classList.contains("cd-visible");
        if (visible) {
            this._drawChanges(changes);
        }
        changes.activity.forEach(moment => this._showActivity(moment, visible));
    }

    /** Let go of what is kept for an agent that left, shown or not. */
    private _forget(id: string, agent: AgentInfo): void {
        this._heartbeats.forget(id); // else churned agents leak dead entries the refresh scans
        this._chat.forgetHistory(agent.id);
    }

    private _drawChanges(changes: AgentChanges): void {
        changes.removed.forEach((_agent, id) => this._animateOut(id));
        const membership = changes.added.size > 0 || changes.removed.size > 0;
        if (this.view === "overview" && changes.added.size > 0) {
            this._overview.renderCards();
        }
        this._patched(changes).forEach(agent => this._overview.patchCard(agent));
        if (this.view === "overview") {
            if (membership || changes.updated.size > 0) {
                this._overview.renderStats();
            }
            if (changes.placement) {
                this._overview.renderNodes();
            }
        }
        if (this.view === "chat" && (membership || changes.updated.size > 0)) {
            this._chat.renderSidebar();
        }
        if (membership) {
            this._chat.updateTargetSelect();
            // The open conversation's agent now shares its name, or no longer:
            // its header and the address name it as the page does, so a reload opens it.
            if (this._chat.relabelled() !== null && this.view === "chat") {
                this._chat.renderChatPaneHeader();
                emit("af-view-changed", { ...this._routes.current(), replace: true });
            }
        }
    }

    /**
     * The cards to repaint: each that changed, and each that shares a name with
     * one that came or went, whose label gains or loses its node with it.
     */
    private _patched(changes: AgentChanges): AgentInfo[] {
        const names = new Set<string>();
        changes.added.forEach(id => names.add(this.store.get(id)?.name ?? ""));
        changes.removed.forEach(agent => names.add(agent.name));
        const ids = new Set(changes.updated);
        names.forEach(name => this.store.byName(name).forEach(agent => ids.add(agent.id)));
        return [...ids].flatMap(id => this.store.get(id) ?? []);
    }

    /** Take a card off with its exit animation. */
    private _animateOut(id: string): void {
        const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
        if (!card) {
            return;
        }
        this._removingIds.add(id);
        card.style.animation = "cd-exit 0.25s ease forwards";
        setTimeout(() => {
            card.remove();
            this._removingIds.delete(id);
        }, 250);
    }

    /** Show a moment on its agent's card. */
    private _showActivity(moment: AgentActivity, visible: boolean): void {
        if (moment.kind === "heartbeat") {
            // Recorded either way; painted only when there is something to paint on.
            // A card mid-removal is animating out, and repainting it fights that.
            this._heartbeats.record(moment.id, moment.timestampMs, {
                skip: !visible || this._removingIds.has(moment.id),
            });
            return;
        }
        const card = visible
            ? this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(moment.id)}"]`)
            : null;
        if (!card) {
            return;
        }
        if (moment.kind === "alert") {
            const severe = moment.severity === "error" || moment.severity === "critical";
            flash(card, severe ? "af-card-alert-error" : "af-card-alert-warn", 900);
        } else {
            flash(card, "af-card-chat-flash", 600);
        }
    }

    /**
     * Fold what the overview fetched into the nodes the store knows, then repaint.
     */
    private _onDataUpdate(): void {
        this.store.mergeNodeListings(this._data.listings.values());
        if (this.view === "overview") {
            this._overview.paintTrends();
            this._overview.renderNodes();
        }
    }

    renderView(): void {
        this._renderView();
    }

    /** Register an extension view (nav button + content builder). An extension
     *  asks for this with `af-register-view`, once, during startup. */
    registerView(key: string, icon: IconName, label: string, builder: () => HTMLElement): void {
        this._viewBuilders.set(key, builder);
        this._extraViews.push({ key, icon, label });
        // Rebuild nav if dashboard is already visible — header buttons
        // were built with the old extraViews list.
        if (this.root.classList.contains("cd-visible")) {
            this._rebuildNav();
        }
        this._routes.registered(key);
    }

    /** Seed runtime config from /api/config and point the Devices nav link at it. */
    private _loadServerConfig(): void {
        seedServerConfig()
            .then(() => {
                setHaNavUrl(this.root, this.haUrl);
                setSignOutVisible(this.root);
            })
            .catch(() => {});
    }

    private _wireEvents(): void {
        this._wireFeedEvents();
        this._chat.wire();
        this._activity.wire();
        this._listeners.on("af-connection-status", detail => {
            this.connState = detail.status;
            renderConnBadge(this.root, this.connState);
            this._renderHealth();
        });
        this._listeners.on("af-route", route => this._routes.open(route));
        this._listeners.on("af-register-view", v => this.registerView(v.key, v.icon, v.label, v.build));
        this._listeners.on("af-model-pointer", d => this._models.moved(d.name));
    }

    private _wireFeedEvents(): void {
        this._listeners.on("af-feed-push", detail => this._activity.push(detail.item));

        this._listeners.on("af-wipe-all", () => {
            this._metrics.setTotalCostUsd(0);
            this._metrics.setTotalMessages(0);
            this._activity.clear();
            this._chat.clearAll();
            this._renderView();
        });

        // Fires once the agent list can be trusted — a reset's survivors applied,
        // or an agent explicitly deleted. The only point at which "the chat
        // target is gone" is a fact rather than a race with re-registration.
        this._listeners.on("af-agents-settled", d => {
            this._chat.dropTargetIfGone(d.reason);
        });

        // A metrics/logs reset cleared the server-side activity log — drop this
        // dashboard's own in-card feed too.
        this._listeners.on("af-clear-feed", () => {
            this._activity.clear();
            this._renderView();
        });
    }

    private _unwireEvents(): void {
        this._chat.unwire();
        this._listeners.off();
    }

    private _renderView(): void {
        const body = this.root.querySelector<HTMLElement>(".af-body")!;
        body.innerHTML = "";
        this._mountActiveView(body);
        // Marked once it is in place, so a test waiting for it waits for the view.
        const mounted = body.lastElementChild;
        if (mounted instanceof HTMLElement) {
            testId(mounted, `view-${this.view}`);
        }
        this._syncViewChrome();
    }

    /** Build and attach the element for the current view. */
    private _mountActiveView(body: HTMLElement): void {
        if (this.view === "overview") {
            body.appendChild(this._overview.build());
        } else if (this.view === "feed") {
            body.appendChild(this._activity.build());
            this._activity.logs.load();
            this._activity.logs.resume();
        } else if (this.view === "settings") {
            body.appendChild(this._buildSettingsView());
        } else if (this.view === "chat") {
            body.appendChild(this._chat.buildChatView());
            this._chat.afterMount();
        } else if (this.view === "models") {
            body.appendChild(this._models.build());
        } else {
            const builder = this._viewBuilders.get(this.view);
            if (builder) {
                body.appendChild(builder());
            }
        }
    }

    /** Sync the header view buttons, health line and target-select to the view. */
    private _syncViewChrome(): void {
        this.root.querySelectorAll<HTMLElement>(".af-view-btn[data-view]").forEach(btn => {
            const active = btn.dataset["view"] === this.view;
            btn.classList.toggle("active", active);
            if (active) {
                btn.setAttribute("aria-current", "page");
            } else {
                btn.removeAttribute("aria-current");
            }
        });
        this._renderHealth();
        // The composer sits outside the view body, so it is on screen on the
        // overview too, where the chat view may never have been mounted. It
        // names the target from here; updateTargetSelect resolves one if none
        // has been chosen yet.
        this._chat.updateTargetSelect();
        // Only show the agent-target dropdown in the chat view. Its value is the
        // one updateTargetSelect gave it, which falls back to an agent that can
        // be shown when the target is not among the options.
        const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
        if (select) {
            select.style.display = this.view === "chat" ? "" : "none";
        }
    }

    private _setView(v: View): void {
        if (v === "chat") {
            this._chat.resolveDefaultTarget();
            // Only on arrival. `_renderView` also runs for state updates while
            // the view is open, and resetting there would reopen the pane on the
            // next render — undoing Back the moment anything changed.
            if (this.view !== "chat") {
                this._chat.showConversation();
            }
        }
        if (this.view === "feed" && v !== "feed") {
            // Leaving the activity view: stop polling for something nobody is
            // looking at. The flag survives, so returning resumes it, and the
            // pushed records are still taken in meanwhile.
            this._activity.pause();
        }
        this.view = v;
        this._routes.moved();
        this._renderView();
        emit("af-view-changed", this._routes.current());
    }

    private _renderHealth(): void {
        const el = this.root.querySelector<HTMLElement>(".af-health");
        if (!el) {
            return;
        }
        const agents = this.store.list();
        const healthy = agents.filter(a => stateLabel(a.state) === "running").length;
        el.textContent = `${healthy}/${agents.length} wactorz healthy`;
    }

    /**
     * Run a card's action, asking first when it cannot be undone. Start and stop
     * are each their own way back; delete is not, so it names the agent and waits
     * for an answer before anything is sent.
     */
    private _sendCommand(id: string, action: AgentAction, btn?: HTMLButtonElement): void {
        if (action !== "delete") {
            this._dispatchCommand(id, action, btn);
            return;
        }
        const agent = this.store.get(id);
        const name = agent ? this.store.label(agent) : id;
        void confirmDialog({
            title: "Delete agent?",
            message: `Are you sure you want to delete ${name}? This cannot be undone.`,
            confirmLabel: "Delete",
        }).then(confirmed => {
            if (confirmed) {
                this._dispatchCommand(id, action, btn);
            }
        });
    }

    private _dispatchCommand(id: string, action: AgentAction, btn?: HTMLButtonElement): void {
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

    private _rebuildNav(): void {
        const oldHeader = this.root.querySelector<HTMLElement>(".af-header");
        const oldBottomNav = this.root.querySelector<HTMLElement>(".af-bottom-nav");
        const onSetView = (v: View) => this._setView(v);
        const haUrl = this.haUrl;

        if (oldHeader) {
            // The header's popovers sit on document.body, so replacing the
            // header does not remove them or their outside-click listeners.
            releaseHeaderPopovers();
            const newHeader = buildHeader({
                view: this.view,
                connState: this.connState,
                onSetView,
                haUrl,
                extraViews: this._extraViews,
            });
            oldHeader.replaceWith(newHeader);
            renderConnBadge(this.root, this.connState);
            this._renderHealth();
        }
        if (oldBottomNav) {
            const newNav = buildBottomNav({
                view: this.view,
                onSetView,
                haUrl,
                extraViews: this._extraViews,
            });
            oldBottomNav.replaceWith(newNav);
        }
    }

    private buildRoot(): HTMLElement {
        const root = el("div", "cd-root");
        root.id = "card-dashboard";

        // The page's main content: whichever view is open.
        const body = el("main", "af-body");

        // The iobar is owned by the chat controller and appended in the constructor.
        const onSetView = (v: View) => this._setView(v);
        const haUrl = this.haUrl;
        root.append(
            buildHeader({
                view: this.view,
                connState: this.connState,
                onSetView,
                haUrl,
                extraViews: this._extraViews,
            }),
            body,
            buildBottomNav({ view: this.view, onSetView, haUrl, extraViews: this._extraViews }),
        );
        return root;
    }

    private _buildSettingsView(): HTMLElement {
        return this._metrics.buildSettingsView();
    }
}

/** Put `cls` on `card` for `ms`. */
function flash(card: HTMLElement, cls: string, ms: number): void {
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls), ms);
}
