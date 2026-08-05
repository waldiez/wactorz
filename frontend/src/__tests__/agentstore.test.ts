/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AgentInfo, HeartbeatPayload, AlertPayload, SpawnPayload } from "../types/agent";

// AgentStore news up a CardDashboard in its constructor and forwards every
// mutation to it. Mock the dashboard so we can isolate the store's logic and
// assert the forwarding.
const dash = vi.hoisted(() => ({
    show: vi.fn(),
    addAgent: vi.fn(),
    updateAgent: vi.fn(),
    removeAgent: vi.fn(),
    setTotalCostUsd: vi.fn(),
    setTotalMessages: vi.fn(),
    updateRemoteNode: vi.fn(),
    setHostStats: vi.fn(),
    onHeartbeat: vi.fn(),
    showAlert: vi.fn(),
    onChat: vi.fn(),
    destroy: vi.fn(),
}));
vi.mock("../ui/CardDashboard", () => ({
    // A class whose constructor returns the shared mock — `new CardDashboard()`
    // then yields `dash`, so all forwarding lands on assertable spies.
    CardDashboard: class {
        constructor() {
            return dash;
        }
    },
}));

import { AgentStore } from "../agents/AgentStore";

function agent(over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: "id-1", name: "worker", state: "running", protected: false, ...over };
}
function hb(over: Partial<HeartbeatPayload> = {}): HeartbeatPayload {
    return {
        agentId: "id-1",
        agentName: "worker",
        state: "running",
        sequence: 1,
        timestampMs: 1_000,
        ...over,
    };
}

let store: AgentStore;
beforeEach(() => {
    vi.clearAllMocks();
    store = new AgentStore();
    // mount() is no longer implicit in the constructor — creating the store has
    // no DOM side effect, so the dashboard is attached explicitly.
    store.mount();
});
afterEach(() => {
    vi.restoreAllMocks();
});

describe("AgentStore — construction", () => {
    it("creates a CardDashboard and shows it empty", () => {
        expect(dash.show).toHaveBeenCalledWith([]);
    });
});

describe("AgentStore — addOrUpdateAgent", () => {
    it("adds a new agent and forwards addAgent", () => {
        store.addOrUpdateAgent(agent());
        expect(dash.addAgent).toHaveBeenCalledOnce();
        expect(store.getAgents()).toHaveLength(1);
    });

    it("merges an existing agent (same id) and forwards updateAgent", () => {
        store.addOrUpdateAgent(agent({ cpu: 1 }));
        store.addOrUpdateAgent(agent({ task: "x" }));
        expect(dash.updateAgent).toHaveBeenCalledOnce();
        expect(store.getAgents()[0]).toMatchObject({ cpu: 1, task: "x" });
    });

    it("evicts a same-name agent with a different id (re-spawn = restart)", () => {
        store.addOrUpdateAgent(agent({ id: "old" }));
        store.addOrUpdateAgent(agent({ id: "new" }));
        expect(dash.removeAgent).toHaveBeenCalledWith("old");
        expect(store.getAgents().map(a => a.id)).toEqual(["new"]);
    });

    it("keeps protected and node sticky across a partial update", () => {
        store.addOrUpdateAgent(agent({ protected: true, node: "n1" }));
        store.addOrUpdateAgent(agent({ protected: false }));
        expect(store.getAgents()[0]).toMatchObject({ protected: true, node: "n1" });
    });
});

describe("AgentStore — removal & totals", () => {
    it("removeAgent drops it and forwards", () => {
        store.addOrUpdateAgent(agent());
        store.removeAgent("id-1");
        expect(dash.removeAgent).toHaveBeenCalledWith("id-1");
        expect(store.getAgents()).toEqual([]);
    });

    it("forwards cost / messages / host stats", () => {
        store.setTotalCostUsd(1.5);
        store.setTotalMessages(7);
        store.setHostStats(50, 1024, 2048);
        expect(dash.setTotalCostUsd).toHaveBeenCalledWith(1.5);
        expect(dash.setTotalMessages).toHaveBeenCalledWith(7);
        expect(dash.setHostStats).toHaveBeenCalledWith(50, 1024, 2048);
    });

    it("clearAll removes every agent and zeroes the totals", () => {
        store.addOrUpdateAgent(agent({ id: "a" }));
        store.addOrUpdateAgent(agent({ id: "b", name: "other" }));
        store.clearAll();
        expect(store.getAgents()).toEqual([]);
        expect(dash.setTotalCostUsd).toHaveBeenLastCalledWith(0);
        expect(dash.setTotalMessages).toHaveBeenLastCalledWith(0);
    });

    it("dispose destroys the dashboard", () => {
        store.dispose();
        expect(dash.destroy).toHaveBeenCalledOnce();
    });
});

describe("AgentStore — remote nodes", () => {
    it("updateRemoteNode forwards, records last-seen, and evicts stale-name agents", () => {
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.updateRemoteNode("n1", ["someone-else"]);
        expect(dash.updateRemoteNode).toHaveBeenCalledWith("n1", ["someone-else"]);
        // remote-1 is no longer in the live list → evicted
        expect(store.getAgents().some(a => a.id === "r1")).toBe(false);
    });

    it("updateRemoteNode with an empty list clears last-seen (node offline)", () => {
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.updateRemoteNode("n1", []);
        expect(store.getAgents().some(a => a.id === "r1")).toBe(false);
    });

    it("pruneStaleRemoteAgents evicts node agents past the stale window, keeps fresh & local", () => {
        const now = 1_000_000;
        vi.spyOn(Date, "now").mockReturnValue(now);
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.addOrUpdateAgent(agent({ id: "local", name: "local-1" })); // no node
        store.updateRemoteNode("n1", ["remote-1"]); // last-seen = now
        store.pruneStaleRemoteAgents(180_000); // not stale yet
        expect(
            store
                .getAgents()
                .map(a => a.id)
                .sort(),
        ).toEqual(["local", "r1"]);

        vi.spyOn(Date, "now").mockReturnValue(now + 200_000); // past the window
        store.pruneStaleRemoteAgents(180_000);
        expect(store.getAgents().map(a => a.id)).toEqual(["local"]); // local untouched
    });
});

describe("AgentStore — reconcileAgents", () => {
    it("evicts local agents missing from the live set, keeps remote, adds live", () => {
        store.addOrUpdateAgent(agent({ id: "gone", name: "gone" }));
        store.addOrUpdateAgent(agent({ id: "remote", name: "remote", node: "n1" }));
        store.reconcileAgents([agent({ id: "kept", name: "kept" })]);
        const ids = store
            .getAgents()
            .map(a => a.id)
            .sort();
        expect(ids).toEqual(["kept", "remote"]); // "gone" evicted, "remote" kept (has node)
    });
});

describe("AgentStore — heartbeat", () => {
    it("updates an existing agent's live fields and forwards onHeartbeat", () => {
        store.addOrUpdateAgent(agent());
        store.onHeartbeat(hb({ state: "paused", cpu: 12, memory_mb: 256, task: "busy" }));
        expect(store.getAgents()[0]).toMatchObject({ state: "paused", cpu: 12, mem: 256, task: "busy" });
        expect(dash.onHeartbeat).toHaveBeenCalledWith("id-1", 1_000, 12, 256);
    });

    it("creates a card for an unknown agent and still pulses onHeartbeat", () => {
        store.onHeartbeat(hb({ agentId: "new", agentName: "newbie", node: "n2" }));
        expect(store.getAgents().map(a => a.id)).toEqual(["new"]);
        expect(store.getAgents()[0]).toMatchObject({ node: "n2" });
        expect(dash.onHeartbeat).toHaveBeenCalledWith("new", 1_000, undefined, undefined);
    });

    it("falls back to Date.now() when the heartbeat timestamp is non-finite", () => {
        vi.spyOn(Date, "now").mockReturnValue(5_000);
        store.onHeartbeat(hb({ agentId: "n", agentName: "n", timestampMs: NaN }));
        expect(store.getAgents()[0]?.lastHeartbeatAt).toBe(new Date(5_000).toISOString());
    });
});

describe("AgentStore — alert / chat / spawn", () => {
    it("onAlert forwards to showAlert", () => {
        const payload: AlertPayload = {
            agentId: "id-1",
            agentName: "worker",
            severity: "error",
            message: "boom",
            timestampMs: 1,
        };
        store.onAlert(payload);
        expect(dash.showAlert).toHaveBeenCalledWith("id-1", "error");
    });

    it("onChat resolves from/to names to ids and forwards", () => {
        store.addOrUpdateAgent(agent({ id: "a", name: "alice" }));
        store.addOrUpdateAgent(agent({ id: "b", name: "bob" }));
        store.onChat("alice", "bob");
        expect(dash.onChat).toHaveBeenCalledWith("a", "b");
    });

    it("onChat with an unknown sender is a no-op", () => {
        store.onChat("ghost", "bob");
        expect(dash.onChat).not.toHaveBeenCalled();
    });

    it("onChat forwards an empty toId when the recipient is unknown", () => {
        store.addOrUpdateAgent(agent({ id: "a", name: "alice" }));
        store.onChat("alice", "nobody");
        expect(dash.onChat).toHaveBeenCalledWith("a", "");
    });

    it("onSpawn adds an initializing agent", () => {
        const payload: SpawnPayload = {
            agentId: "s1",
            agentName: "spawned",
            agentType: "dynamic",
            timestampMs: 1,
            protected: true,
        };
        store.onSpawn(payload);
        expect(store.getAgents()[0]).toMatchObject({
            id: "s1",
            name: "spawned",
            state: "initializing",
            protected: true,
            agentType: "dynamic",
        });
    });
});
