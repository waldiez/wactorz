/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Agent-state store + dashboard coordinator. Owns the canonical Map of live
 * agents (keyed by WID id) and drives the CardDashboard from MQTT/WS events:
 * every mutation here forwards the relevant add/update/remove to the dashboard.
 *
 * An agent leaves the store when it is deleted: the server's delete frame, the
 * user's delete, a wipe. Every ending sends one of those, so nothing here
 * removes an agent merely because one source stopped listing it -- that
 * guess was undone by the agent's next heartbeat, and the card blinked. Only
 * an agent that has also gone silent for long is let go, for the delete frame
 * that never arrived; until then its card's dot says how long it has been.
 */

import type { AgentInfo, HeartbeatPayload, AlertPayload, SpawnPayload, NodeReadings } from "../types/agent";
import { CardDashboard } from "../ui/CardDashboard";
import { NODE_EVICT_MS, STALE_MS } from "../ui/dashboard/agentState";

export class AgentStore {
    private agents: Map<string, AgentInfo> = new Map();
    private cardDashboard: CardDashboard | null = null;
    /** When each agent was last heard of, from any source: an event, or the server's list. */
    private _lastHeard: Map<string, number> = new Map();

    /** Build and show the (initially empty) CardDashboard.
     *
     * Separate from the constructor so that creating the store has no DOM side
     * effect: every method here already tolerates `cardDashboard === null`, so
     * the field was designed to be optional and only construction forced it.
     * Calling twice would strand the first dashboard, so it is a no-op after
     * the first.
     */
    mount(): void {
        if (this.cardDashboard) {
            return;
        }
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
        this._lastHeard.set(agent.id, Date.now());
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
        this._lastHeard.delete(id);
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
     * Refresh a remote node's agent list and readings on the dashboard.
     *
     * The list is not used to remove agents: one missing from a single
     * heartbeat -- restarting, or listed under a slightly different name -- is
     * not gone, and an agent that is gone is deleted by the server.
     */
    updateRemoteNode(name: string, agents: string[], readings?: NodeReadings): void {
        this.cardDashboard?.updateRemoteNode(name, agents, readings);
    }

    /** Push host CPU/memory stats to the dashboard. */
    setHostStats(cpu: number, memUsedMb: number, memTotalMb?: number): void {
        this.cardDashboard?.setHostStats(cpu, memUsedMb, memTotalMb);
    }

    /**
     * Merge in the server's list of its own agents.
     *
     * An agent missing from it is removed only once it has also been silent
     * past `STALE_MS`. The list is not the whole picture: an agent that is
     * ending itself has left it before its delete frame arrives, and an agent
     * of another server on the same broker is never in it while its heartbeats
     * keep arriving. Removing either on the list alone put the card back on the
     * next heartbeat. Agents on a node are never in the list and are left to
     * `pruneSilentRemoteAgents`.
     */
    reconcileAgents(liveAgents: AgentInfo[], now = Date.now()): void {
        const liveIds = new Set(liveAgents.map(agent => agent.id));
        const toEvict: string[] = [];
        for (const [id, agent] of this.agents) {
            if (!liveIds.has(id) && !agent.node && this._silentFor(id, now) > STALE_MS) {
                toEvict.push(id);
            }
        }
        toEvict.forEach(id => this.removeAgent(id));
        liveAgents.forEach(agent => this.addOrUpdateAgent(agent));
    }

    /**
     * Let go of agents on a node that have been silent past ``silentMs``.
     *
     * For a delete that never reached this page. The default is how long the
     * nodes panel keeps a node it has stopped hearing from, so an agent goes
     * when its node does; before then its card's dot shows it is missing.
     */
    pruneSilentRemoteAgents(silentMs = NODE_EVICT_MS, now = Date.now()): void {
        const toEvict = [...this.agents]
            .filter(([id, agent]) => agent.node && this._silentFor(id, now) > silentMs)
            .map(([id]) => id);
        toEvict.forEach(id => this.removeAgent(id));
    }

    /** How long since anything was heard of an agent; forever for one never heard of. */
    private _silentFor(id: string, now: number): number {
        const heard = this._lastHeard.get(id);
        return heard === undefined ? Infinity : now - heard;
    }

    /**
     * Apply a heartbeat: update an existing agent's state/metrics, or create it
     * if unknown, then pulse its card.
     */
    onHeartbeat(payload: HeartbeatPayload): void {
        const agent = this.agents.get(payload.agentId);
        if (agent) {
            this._lastHeard.set(payload.agentId, Date.now());
            agent.state = payload.state;
            // A card first made from an event that did not say where the agent
            // runs (a spawn) learns it here, so it is not taken for a local
            // agent the server has forgotten.
            if (payload.node !== undefined && agent.node !== payload.node) {
                agent.node = payload.node;
            }
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
