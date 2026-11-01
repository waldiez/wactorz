/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The page's agents and the nodes they run on: what is known of each, and
 * nothing about how it is drawn.
 *
 * Keyed by id. Two agents may share a name -- the same agent on two nodes --
 * so a name is never taken as an identity: a second agent of a name is a
 * second agent, not a restart of the first.
 *
 * Views observe it with `subscribe`. Changes are told in one batch per
 * microtask, so a reconcile or a wipe that touches every agent is drawn once
 * rather than once per agent. A record is replaced, never changed where it
 * stands: whoever holds the previous one can tell what changed.
 *
 * An agent leaves when it is deleted: the server's delete frame, the user's
 * delete, a wipe. Every ending sends one of those, so nothing here removes an
 * agent merely because one source stopped listing it -- that guess was undone
 * by the agent's next heartbeat, and the card blinked. Only an agent that has
 * also gone silent for long is let go, for the delete frame that never
 * arrived; until then its card's dot says how long it has been.
 */

import type {
    AgentInfo,
    AlertPayload,
    HeartbeatPayload,
    NodeListing,
    NodeReadings,
    RemoteNode,
    SpawnPayload,
} from "../types/agent";
import { readingsFrom } from "./nodeReadings";
import { NODE_EVICT_MS, STALE_MS } from "./liveness";
import { addressOf } from "./addresses";

/** A moment worth showing on an agent's card, as it happened. */
export type AgentActivity =
    | { kind: "heartbeat"; id: string; timestampMs: number }
    | { kind: "alert"; id: string; severity: string }
    | { kind: "chat"; id: string };

/** What one batch of changes did. */
export interface AgentChanges {
    /** Agents that arrived. */
    readonly added: ReadonlySet<string>;
    /** Agents whose record changed, and that were not added in this batch. */
    readonly updated: ReadonlySet<string>;
    /** Agents that left, as they last were. */
    readonly removed: ReadonlyMap<string, AgentInfo>;
    /** Whether where agents run may look different: an agent came, went or
     *  moved, or a node reported or was let go. */
    readonly placement: boolean;
    /** Card moments, in the order they happened. */
    readonly activity: readonly AgentActivity[];
}

/** Told each batch of changes. */
export type AgentListener = (changes: AgentChanges) => void;

/** Whether a heartbeat changed something a view shows, beyond when it came. */
function visiblyChanged(before: AgentInfo, after: AgentInfo): boolean {
    return before.state !== after.state || before.node !== after.node || before.task !== after.task;
}

/** Whether a listed node says something about itself the page does not hold yet. */
function listingIsNews(known: RemoteNode, listing: NodeListing): boolean {
    return JSON.stringify(known.manifest ?? null) !== JSON.stringify(listing.manifest ?? null);
}

/** The agent names a node listing carries, without whatever else is in its list. */
function listedAgents(listing: NodeListing): string[] {
    const agents = listing["agents"];
    return Array.isArray(agents) ? agents.filter((a): a is string => typeof a === "string") : [];
}

export class AgentStore {
    private readonly _agents = new Map<string, AgentInfo>();
    /** When each agent was last heard of, from any source: an event, or the server's list. */
    private readonly _lastHeard = new Map<string, number>();
    private readonly _nodes = new Map<string, RemoteNode>();
    private readonly _listeners = new Set<AgentListener>();

    private _added = new Set<string>();
    private _updated = new Set<string>();
    private _removed = new Map<string, AgentInfo>();
    private _placement = false;
    private _activity: AgentActivity[] = [];
    private _flushQueued = false;

    // ── Reading ──────────────────────────────────────────────────────────────

    /** Every agent, by id. Read-only: changes go through the methods below. */
    get agents(): ReadonlyMap<string, AgentInfo> {
        return this._agents;
    }

    /** Every remote node heard of, by name. */
    get nodes(): ReadonlyMap<string, RemoteNode> {
        return this._nodes;
    }

    /** The agent `id`, or undefined. */
    get(id: string): AgentInfo | undefined {
        return this._agents.get(id);
    }

    /** Every agent called `name`: none, one, or several on different nodes. */
    byName(name: string): AgentInfo[] {
        return this.list().filter(agent => agent.name === name);
    }

    /** All agents, in the order they arrived. */
    list(): AgentInfo[] {
        return [...this._agents.values()];
    }

    /**
     * What to call `agent` on screen: its name while no other agent has it,
     * `name@node` once one does. Main is the node `main`.
     */
    label(agent: AgentInfo): string {
        if (this.byName(agent.name).length < 2) {
            return agent.name;
        }
        return addressOf(agent);
    }

    /** Be told each batch of changes; returns the way to stop. */
    subscribe(listener: AgentListener): () => void {
        this._listeners.add(listener);
        return () => this._listeners.delete(listener);
    }

    // ── Agents ───────────────────────────────────────────────────────────────

    /**
     * Insert or merge an agent by id. `protected` and `node` are sticky:
     * partial updates do not carry them and must not clear them.
     */
    addOrUpdateAgent(agent: AgentInfo): void {
        const existing = this._agents.get(agent.id);
        const merged: AgentInfo = existing ? { ...existing, ...agent } : agent;
        if (existing?.protected) {
            merged.protected = true;
        }
        if (existing?.node) {
            merged.node = existing.node;
        }
        this._lastHeard.set(agent.id, Date.now());
        this._put(merged, existing);
    }

    /** Drop an agent by id. */
    removeAgent(id: string): void {
        const removed = this._agents.get(id);
        this._lastHeard.delete(id);
        if (!removed) {
            return;
        }
        this._agents.delete(id);
        // One that came and went within a batch was never drawn, so it is not undrawn either.
        if (!this._added.delete(id)) {
            this._removed.set(id, removed);
        }
        this._updated.delete(id);
        this._placement = true;
        this._queue();
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
        const toEvict = this.list()
            .filter(
                agent => !liveIds.has(agent.id) && !agent.node && this._silentFor(agent.id, now) > STALE_MS,
            )
            .map(agent => agent.id);
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
        const toEvict = this.list()
            .filter(agent => agent.node && this._silentFor(agent.id, now) > silentMs)
            .map(agent => agent.id);
        toEvict.forEach(id => this.removeAgent(id));
    }

    /**
     * Apply a heartbeat: the agent's state, where it runs and what it is
     * doing, creating it if unknown; and a pulse on its card.
     */
    onHeartbeat(payload: HeartbeatPayload): void {
        const existing = this._agents.get(payload.agentId);
        const timestampMs = Number.isFinite(payload.timestampMs) ? payload.timestampMs : Date.now();
        const next: AgentInfo = {
            ...(existing ?? { id: payload.agentId, name: payload.agentName, protected: false }),
            state: payload.state,
            lastHeartbeatAt: new Date(timestampMs).toISOString(),
            // Where it runs, from the agent itself: a card first made from an
            // event that did not say (a spawn) learns it here, and one that moved
            // home learns it is local ("") -- which the nodes panel then shows.
            ...(payload.node !== undefined && { node: payload.node }),
            ...(payload.cpu !== undefined && { cpu: payload.cpu }),
            ...(payload.memory_mb !== undefined && { mem: payload.memory_mb }),
            ...(payload.task !== undefined && { task: payload.task }),
        };
        this._lastHeard.set(payload.agentId, Date.now());
        this._put(next, existing, existing === undefined || visiblyChanged(existing, next));
        this._note({ kind: "heartbeat", id: payload.agentId, timestampMs: payload.timestampMs });
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

    /** An agent raised an alert: its card shows it for a moment. */
    onAlert(payload: AlertPayload): void {
        this._note({ kind: "alert", id: payload.agentId, severity: payload.severity });
    }

    /**
     * `fromName` sent a chat message: its card shows it for a moment. Nothing
     * shows when the name is not one agent's: picking one of several would
     * light the wrong card as often as the right one.
     */
    onChat(fromName: string): void {
        const senders = this.byName(fromName);
        if (senders.length === 1) {
            this._note({ kind: "chat", id: senders[0]!.id });
        }
    }

    /** Remove every agent. */
    clearAll(): void {
        this.list().forEach(agent => this.removeAgent(agent.id));
    }

    // ── Nodes ────────────────────────────────────────────────────────────────

    /**
     * Record a remote node's report: the agents it listed, and its readings.
     *
     * The list is not used to remove agents: one missing from a single
     * heartbeat -- restarting, or listed under a slightly different name -- is
     * not gone, and an agent that is gone is deleted by the server.
     */
    updateRemoteNode(name: string, agents: string[], readings?: NodeReadings, now = Date.now()): void {
        // The manifest comes from the server's listing, not the heartbeat; keep it.
        const manifest = this._nodes.get(name)?.manifest;
        this._nodes.set(name, {
            agents,
            lastSeen: now,
            ...(readings !== undefined && { readings }),
            ...(manifest !== undefined && { manifest }),
        });
        // Nodes announce themselves and never say goodbye, so without this the
        // map only ever grows. Going quiet is not enough to be dropped -- the
        // panel renders quiet nodes as offline, which is information -- but a
        // node absent this long is gone, not offline.
        for (const [node, info] of this._nodes) {
            if (node !== name && now - info.lastSeen > NODE_EVICT_MS) {
                this._nodes.delete(node);
            }
        }
        this._placement = true;
        this._queue();
    }

    /**
     * Fold in the server's listing of nodes.
     *
     * The listing carries each node's manifest, and also names nodes this page
     * has not heard a heartbeat from yet -- it opened after their last one --
     * which it then holds from the listing until one arrives.
     */
    mergeNodeListings(listings: Iterable<NodeListing>): void {
        let news = false;
        for (const listing of listings) {
            const known = this._nodes.get(listing.node);
            const manifest = listing.manifest ?? null;
            if (known && !listingIsNews(known, listing)) {
                continue;
            }
            news = true;
            this._nodes.set(
                listing.node,
                known
                    ? { ...known, manifest }
                    : {
                          agents: listedAgents(listing),
                          lastSeen: (listing.last_seen ?? 0) * 1000,
                          readings: readingsFrom(listing),
                          manifest,
                      },
            );
        }
        if (news) {
            this._placement = true;
            this._queue();
        }
    }

    // ── Batching ─────────────────────────────────────────────────────────────

    /** Store `next` in place of `before`, counting it as a change when `shown`. */
    private _put(next: AgentInfo, before: AgentInfo | undefined, shown = true): void {
        this._agents.set(next.id, next);
        if (before === undefined && this._removed.delete(next.id)) {
            // Gone and back within a batch (a wipe's survivors): still on screen,
            // so it is drawn again rather than taken off and put back.
            this._updated.add(next.id);
            this._placement = true;
        } else if (before === undefined) {
            this._added.add(next.id);
            this._placement = true;
        } else if (shown && !this._added.has(next.id)) {
            this._updated.add(next.id);
        }
        if (before !== undefined && (before.node ?? "") !== (next.node ?? "")) {
            this._placement = true;
        }
        if (shown) {
            this._queue();
        }
    }

    private _note(activity: AgentActivity): void {
        this._activity.push(activity);
        this._queue();
    }

    private _queue(): void {
        if (this._flushQueued) {
            return;
        }
        this._flushQueued = true;
        queueMicrotask(() => this._flush());
    }

    private _flush(): void {
        this._flushQueued = false;
        const changes: AgentChanges = {
            added: this._added,
            updated: this._updated,
            removed: this._removed,
            placement: this._placement,
            activity: this._activity,
        };
        this._added = new Set();
        this._updated = new Set();
        this._removed = new Map();
        this._placement = false;
        this._activity = [];
        this._listeners.forEach(listener => listener(changes));
    }

    /** How long since anything was heard of an agent; forever for one never heard of. */
    private _silentFor(id: string, now: number): number {
        const heard = this._lastHeard.get(id);
        return heard === undefined ? Infinity : now - heard;
    }
}
