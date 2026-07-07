/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Overview view of the dashboard: the host-resource bar, summary stat cards,
 * the per-agent "wactor" card grid and the nodes panel. Reads shared state
 * through the host and routes card interactions back via `onChat` / `onCommand`.
 */
import type { AgentInfo } from "../../types/agent";
import { stateColor, stateLabel, sortAgents, STALE_MS } from "./agentState";
import {
    buildHostBar,
    buildStatCards,
    buildWactorCard,
    appendActionBtns,
    type StatCardData,
    type AgentAction,
} from "./cards";

export interface OverviewHost {
    readonly root: HTMLElement;
    readonly agents: Map<string, AgentInfo>;
    readonly lastHb: Map<string, number>;
    readonly remoteNodes: Map<string, { agents: string[]; lastSeen: number }>;
    readonly removingIds: Set<string>;
    /** [cpu, memUsedMb, memTotalMb] for the host bar. */
    hostStats(): [number | null, number | null, number | null];
    /** Inputs for the summary stat cards. */
    statData(): StatCardData;
    /** Open chat targeting the named agent. */
    onChat(name: string): void;
    /** Run a control command (pause/resume/stop/delete) on an agent. */
    onCommand(id: string, action: AgentAction, btn: HTMLButtonElement): void;
}

export class OverviewView {
    constructor(private host: OverviewHost) {}

    /** Build the full overview element: host bar, stat cards, wactor grid and nodes panel. */
    build(): HTMLElement {
        const el = document.createElement("div");
        el.className = "af-overview";

        const [cpu, memUsed, memTotal] = this.host.hostStats();
        el.appendChild(buildHostBar(cpu, memUsed, memTotal));

        const statsGrid = document.createElement("div");
        statsGrid.className = "af-stats-grid";
        statsGrid.id = "af-stats-grid";
        buildStatCards(statsGrid, this.host.statData());
        el.appendChild(statsGrid);

        const panels = document.createElement("div");
        panels.className = "af-overview-panels";
        panels.append(this._buildWactorPanel(), this._buildNodesPanel());
        el.appendChild(panels);
        return el;
    }

    /** Re-render the summary stat cards in place (no-op if not mounted). */
    renderStats(): void {
        const grid = this.host.root.querySelector<HTMLElement>("#af-stats-grid");
        if (grid) {
            buildStatCards(grid, this.host.statData());
        }
    }

    /** Reconcile the wactor card grid: remove dead cards and add new ones (sorted). */
    renderCards(): void {
        const grid = this.host.root.querySelector<HTMLElement>("#af-wactor-cards");
        if (!grid) {
            return;
        }
        const sorted = sortAgents(this.host.agents.values());
        const live = new Set(sorted.map(a => a.id));
        grid.querySelectorAll<HTMLElement>("[data-id]").forEach(el => {
            if (!live.has(el.dataset["id"]!)) {
                this.host.removingIds.delete(el.dataset["id"]!);
                el.remove();
            }
        });
        sorted.forEach(agent => {
            if (!grid.querySelector(`[data-id="${CSS.escape(agent.id)}"]`)) {
                grid.appendChild(this._buildCard(agent));
            }
        });
    }

    /** Update one card's state dot/label/name/controls in place, rebuilding the grid if it's missing. */
    patchCard(agent: AgentInfo): void {
        if (this.host.removingIds.has(agent.id)) {
            return;
        }
        const card = this.host.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agent.id)}"]`);
        if (!card) {
            this.renderCards();
            return;
        }
        const color = stateColor(agent.state);
        const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
        const lbl = card.querySelector<HTMLElement>(".af-card-state-label");
        const nm = card.querySelector<HTMLElement>(".af-card-name");
        if (dot) {
            dot.style.background = color;
            dot.style.boxShadow = `0 0 8px ${color}`;
        }
        if (lbl) {
            lbl.style.color = color;
            lbl.textContent = stateLabel(agent.state);
        }
        if (nm) {
            nm.textContent = agent.name;
        }
        this._rebuildControls(card, agent);
    }

    /** Render the nodes panel (local + remote nodes with online/offline pills) into `container` or the mounted list. */
    renderNodes(container?: HTMLElement): void {
        const list = container ?? this.host.root.querySelector<HTMLElement>("#af-node-list");
        if (!list) {
            return;
        }
        // Remote-runner agents carry a `node` and belong to that remote node's list;
        // the local node lists only agents running here (no `node`).
        const agentNames = [...this.host.agents.values()].filter(a => !a.node).map(a => a.name);
        const items: HTMLElement[] = [
            this._buildNodeItem("local", agentNames.length > 0 ? agentNames.join(", ") : "no agents", true),
        ];
        const now = Date.now();
        for (const [name, info] of this.host.remoteNodes) {
            const online = now - info.lastSeen < STALE_MS;
            const meta = info.agents.length > 0 ? info.agents.join(", ") : "no agents";
            items.push(this._buildNodeItem(name, meta, online));
        }
        list.replaceChildren(...items);
    }

    /**
     * Build one node row. Node/agent names arrive from untrusted MQTT topics, so
     * all dynamic text is set via `textContent` — never interpolated into HTML.
     */
    private _buildNodeItem(name: string, meta: string, online: boolean): HTMLElement {
        const item = document.createElement("div");
        item.className = "af-node-item";

        const info = document.createElement("div");
        const nameEl = document.createElement("div");
        nameEl.className = "af-node-name";
        nameEl.textContent = name;
        const metaEl = document.createElement("div");
        metaEl.className = "af-node-meta";
        metaEl.textContent = meta;
        info.append(nameEl, metaEl);

        const pill = document.createElement("span");
        pill.className = `af-node-pill ${online ? "online" : "offline"}`;
        pill.textContent = online ? "online" : "offline";

        item.append(info, pill);
        return item;
    }

    private _buildWactorPanel(): HTMLElement {
        const wp = document.createElement("section");
        wp.className = "af-panel";
        wp.innerHTML = `<div class="af-panel-head"><h3>Wactorz</h3><span>actor model · MQTT pub-sub</span></div>`;
        const grid = document.createElement("div");
        grid.className = "af-cards-grid";
        grid.id = "af-wactor-cards";
        sortAgents(this.host.agents.values()).forEach(agent => grid.appendChild(this._buildCard(agent)));
        wp.appendChild(grid);
        return wp;
    }

    private _buildNodesPanel(): HTMLElement {
        const np = document.createElement("section");
        np.className = "af-panel";
        np.innerHTML = `<div class="af-panel-head"><h3>Nodes</h3><span>from heartbeat telemetry</span></div>`;
        const nodeList = document.createElement("div");
        nodeList.className = "af-node-list";
        nodeList.id = "af-node-list";
        np.appendChild(nodeList);
        this.renderNodes(nodeList);
        return np;
    }

    private _buildCard(agent: AgentInfo): HTMLElement {
        return buildWactorCard(agent, this.host.lastHb.get(agent.id) ?? 0, {
            onChat: a => this.host.onChat(a.name),
            onCommand: (id, action, btn) => this.host.onCommand(id, action, btn),
        });
    }

    private _rebuildControls(card: HTMLElement, agent: AgentInfo): void {
        const controls = card.querySelector<HTMLElement>(".af-card-controls");
        if (!controls) {
            return;
        }
        const chatBtn = controls.querySelector<HTMLButtonElement>(".af-chat-btn");
        if (chatBtn) {
            chatBtn.hidden = stateLabel(agent.state) === "stopped";
        }
        // Only replace the action buttons — the delegated click listener stays.
        controls.querySelectorAll("[data-action]").forEach(b => b.remove());
        appendActionBtns(controls, agent);
    }
}
