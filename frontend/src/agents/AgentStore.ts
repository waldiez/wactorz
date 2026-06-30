/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Agent-state store + dashboard coordinator. Owns the canonical Map of live
 * agents (keyed by WID id) and drives the CardDashboard from MQTT/WS events:
 * every mutation here forwards the relevant add/update/remove to the dashboard.
 */

import type { AgentInfo, HeartbeatPayload, AlertPayload, SpawnPayload } from "../types/agent";
import { CardDashboard } from "../ui/CardDashboard";
import { STALE_MS } from "../ui/dashboard/agentState";

export class AgentStore {
    private agents: Map<string, AgentInfo> = new Map();
    private cardDashboard: CardDashboard | null = null;
    private _remoteNodeLastSeen: Map<string, number> = new Map();

    /** Create the store and mount an (initially empty) CardDashboard. */
    constructor() {
        this.cardDashboard = new CardDashboard();
        this.cardDashboard.show([...this.agents.values()]);
    }

    /**
     * Insert or merge an agent by id, forwarding the change to the dashboard.
     * A same-name agent under a different id is treated as a restart and evicted
     * first; `protected` and `node` are sticky across partial MQTT updates.
     */
    addOrUpdateAgent(agent: AgentInfo): void {
        // If another agent with the same NAME but a different ID exists, drop it
        // first — a re-spawn produces a new WID id, treated as a restart.
        for (const [oldId, oldAgent] of this.agents) {
            if (oldAgent.name === agent.name && oldId !== agent.id) {
                this.agents.delete(oldId);
                this.cardDashboard?.removeAgent(oldId);
                break;
            }
        }

        const existing = this.agents.get(agent.id);
        const merged: AgentInfo = existing ? { ...existing, ...agent } : agent;
        // protected:true and node are sticky — partial MQTT updates must not clear them.
        if (existing?.protected) {
            merged.protected = true;
        }
        if (existing?.node) {
            merged.node = existing.node;
        }
        this.agents.set(agent.id, merged);

        if (this.cardDashboard) {
            existing ? this.cardDashboard.updateAgent(merged) : this.cardDashboard.addAgent(merged);
        }
    }

    /** Drop an agent by id and remove its card. */
    removeAgent(id: string): void {
        this.agents.delete(id);
        this.cardDashboard?.removeAgent(id);
    }

    /** Update the dashboard's aggregate cost figure. */
    setTotalCostUsd(usd: number): void {
        this.cardDashboard?.setTotalCostUsd(usd);
    }

    /** Update the dashboard's aggregate message count. */
    setTotalMessages(count: number): void {
        this.cardDashboard?.setTotalMessages(count);
    }

    /**
     * Refresh a remote node's agent list, tracking its last-seen time and
     * evicting remote agents this node no longer reports.
     */
    updateRemoteNode(name: string, agents: string[]): void {
        this.cardDashboard?.updateRemoteNode(name, agents);
        if (agents.length > 0) {
            this._remoteNodeLastSeen.set(name, Date.now());
        } else {
            this._remoteNodeLastSeen.delete(name);
        }
        // Evict remote agents for this node whose names are no longer in the live list.
        const liveNames = new Set(agents);
        const toEvict: string[] = [];
        for (const [id, agent] of this.agents) {
            if (agent.node === name && !liveNames.has(agent.name)) {
                toEvict.push(id);
            }
        }
        toEvict.forEach(id => this.removeAgent(id));
    }

    /** Push host CPU/memory stats to the dashboard. */
    setHostStats(cpu: number, memUsedMb: number, memTotalMb?: number): void {
        this.cardDashboard?.setHostStats(cpu, memUsedMb, memTotalMb);
    }

    /**
     * Replace local (non-remote) agents with the authoritative REST list:
     * evict any not present, then add or update the rest.
     */
    reconcileAgents(liveAgents: AgentInfo[]): void {
        const liveIds = new Set(liveAgents.map(agent => agent.id));
        for (const [id, agent] of this.agents) {
            // Remote agents aren't in the local REST response — evicted elsewhere.
            if (!liveIds.has(id) && !agent.node) {
                this.removeAgent(id);
            }
        }
        liveAgents.forEach(agent => this.addOrUpdateAgent(agent));
    }

    /** Remove agents belonging to nodes whose heartbeat has gone stale (>3 min). */
    pruneStaleRemoteAgents(staleMs = STALE_MS): void {
        const now = Date.now();
        const toEvict: string[] = [];
        for (const [id, agent] of this.agents) {
            if (!agent.node) {
                continue;
            }
            const lastSeen = this._remoteNodeLastSeen.get(agent.node);
            if (lastSeen !== undefined && now - lastSeen > staleMs) {
                toEvict.push(id);
            }
        }
        toEvict.forEach(id => this.removeAgent(id));
    }

    /**
     * Apply a heartbeat: update an existing agent's state/metrics, or create it
     * if unknown, then pulse its card.
     */
    onHeartbeat(payload: HeartbeatPayload): void {
        const agent = this.agents.get(payload.agentId);
        if (agent) {
            agent.state = payload.state;
            agent.lastHeartbeatAt = new Date(payload.timestampMs).toISOString();
            if (payload.cpu !== undefined) {
                agent.cpu = payload.cpu;
            }
            if (payload.memory_mb !== undefined) {
                agent.mem = payload.memory_mb;
            }
            if (payload.task !== undefined) {
                agent.task = payload.task;
            }
            this.cardDashboard?.onHeartbeat(
                payload.agentId,
                payload.timestampMs,
                payload.cpu,
                payload.memory_mb,
            );
        } else {
            this.addOrUpdateAgent({
                id: payload.agentId,
                name: payload.agentName,
                state: payload.state,
                protected: false,
                lastHeartbeatAt: new Date(
                    Number.isFinite(payload.timestampMs) ? payload.timestampMs : Date.now(),
                ).toISOString(),
                ...(payload.node !== undefined && { node: payload.node }),
            });
            // Pulse the newly created card immediately (avoid a ~10s blink gap).
            this.cardDashboard?.onHeartbeat(
                payload.agentId,
                payload.timestampMs,
                payload.cpu,
                payload.memory_mb,
            );
        }
    }

    /** Flash an alert badge on an agent's card. */
    onAlert(payload: AlertPayload): void {
        this.cardDashboard?.showAlert(payload.agentId, payload.severity);
    }

    /** Animate a chat edge between two agents resolved by name (sender required). */
    onChat(fromName: string, toName: string): void {
        let fromId: string | undefined;
        let toId: string | undefined;
        for (const agent of this.agents.values()) {
            if (agent.name === fromName) {
                fromId = agent.id;
            }
            if (agent.name === toName) {
                toId = agent.id;
            }
        }
        if (!fromId) {
            return;
        }
        this.cardDashboard?.onChat(fromId, toId ?? "");
    }

    /** Register a newly spawned agent in the `initializing` state. */
    onSpawn(payload: SpawnPayload): void {
        this.addOrUpdateAgent({
            id: payload.agentId,
            name: payload.agentName,
            state: "initializing",
            protected: payload.protected ?? false,
            agentType: payload.agentType,
        });
    }

    /** Return all currently tracked agents (for mention-autocomplete etc.). */
    getAgents(): AgentInfo[] {
        return [...this.agents.values()];
    }

    /** Remove every agent and reset the aggregate cost/message counters. */
    clearAll(): void {
        for (const id of [...this.agents.keys()]) {
            this.removeAgent(id);
        }
        this.setTotalCostUsd(0);
        this.setTotalMessages(0);
    }

    /** Tear down the dashboard (used on page unload). */
    dispose(): void {
        this.cardDashboard?.destroy();
    }
}
