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
import { NODE_EVICT_MS, STALE_MS } from "../ui/dashboard/agentState";

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

const ids = (): string[] =>
    store
        .getAgents()
        .map(a => a.id)
        .sort();

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
    it("updateRemoteNode forwards the node's list and readings", () => {
        store.updateRemoteNode("n1", ["someone-else"], { cpu_pct: 12 });
        expect(dash.updateRemoteNode).toHaveBeenCalledWith("n1", ["someone-else"], { cpu_pct: 12 });
    });

    it("keeps a node's agent that one heartbeat did not list", () => {
        // Restarting, or listed under a slightly different name: not gone. An
        // agent that is gone is deleted by the server.
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.updateRemoteNode("n1", ["someone-else"]);
        store.updateRemoteNode("n1", []);
        expect(store.getAgents().map(a => a.id)).toEqual(["r1"]);
        expect(dash.removeAgent).not.toHaveBeenCalled();
    });

    it("lets a node's agent go only after the long silence its node leaves the panel at", () => {
        const now = 1_000_000;
        vi.spyOn(Date, "now").mockReturnValue(now);
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.addOrUpdateAgent(agent({ id: "local", name: "local-1" }));

        store.pruneSilentRemoteAgents(undefined, now + STALE_MS * 2);
        expect(ids()).toEqual(["local", "r1"]); // missing, not yet gone

        store.pruneSilentRemoteAgents(undefined, now + NODE_EVICT_MS + 1);
        expect(ids()).toEqual(["local"]); // a local agent is not this method's to judge
    });

    it("counts a heartbeat as hearing from a node's agent", () => {
        vi.spyOn(Date, "now").mockReturnValue(1_000);
        store.addOrUpdateAgent(agent({ id: "id-1", name: "worker", node: "n1" }));
        vi.spyOn(Date, "now").mockReturnValue(1_000 + NODE_EVICT_MS);
        store.onHeartbeat(hb());

        store.pruneSilentRemoteAgents(undefined, 1_000 + NODE_EVICT_MS + 10);

        expect(ids()).toEqual(["id-1"]);
    });
});

describe("AgentStore — reconcileAgents", () => {
    it("adds and updates what the server lists", () => {
        store.reconcileAgents([agent({ id: "kept", name: "kept" })]);
        expect(ids()).toEqual(["kept"]);
    });

    it("keeps an unlisted agent it has heard from lately", () => {
        // An agent ending itself has left the server's list before its delete
        // frame arrives; another server's agent on the same broker is never in
        // it. Removing either on the list alone made the card blink.
        const now = 1_000_000;
        vi.spyOn(Date, "now").mockReturnValue(now);
        store.addOrUpdateAgent(agent({ id: "ending", name: "ending" }));

        store.reconcileAgents([], now + STALE_MS - 1);

        expect(ids()).toEqual(["ending"]);
        expect(dash.removeAgent).not.toHaveBeenCalled();
    });

    it("lets go of an unlisted local agent once it has also been silent past the stale window", () => {
        // For the delete frame that never arrived.
        const now = 1_000_000;
        vi.spyOn(Date, "now").mockReturnValue(now);
        store.addOrUpdateAgent(agent({ id: "gone", name: "gone" }));
        store.addOrUpdateAgent(agent({ id: "remote", name: "remote", node: "n1" }));

        store.reconcileAgents([agent({ id: "kept", name: "kept" })], now + STALE_MS + 1);

        expect(ids()).toEqual(["kept", "remote"]); // a node's agent is never in the list
        expect(dash.removeAgent).toHaveBeenCalledWith("gone");
    });

    it("keeps an agent heard from again, however long ago it was first seen", () => {
        // Another server's agent: never listed here, heartbeating all along.
        vi.spyOn(Date, "now").mockReturnValue(1_000);
        store.addOrUpdateAgent(agent({ id: "id-1", name: "worker" }));
        vi.spyOn(Date, "now").mockReturnValue(1_000 + STALE_MS * 5);
        store.onHeartbeat(hb());

        store.reconcileAgents([], 1_000 + STALE_MS * 5 + 10);

        expect(ids()).toEqual(["id-1"]);
    });

    it("still removes an agent at once when it is deleted", () => {
        store.addOrUpdateAgent(agent({ id: "x", name: "x" }));
        store.removeAgent("x");
        expect(ids()).toEqual([]);
    });
});

describe("AgentStore — heartbeat", () => {
    it("updates an existing agent's live fields and forwards onHeartbeat", () => {
        store.addOrUpdateAgent(agent());
        store.onHeartbeat(hb({ state: "stopped", cpu: 12, memory_mb: 256, task: "busy" }));
        expect(store.getAgents()[0]).toMatchObject({ state: "stopped", cpu: 12, mem: 256, task: "busy" });
        expect(dash.onHeartbeat).toHaveBeenCalledWith("id-1", 1_000, 12, 256);
    });

    it("learns where an existing agent runs from a later heartbeat", () => {
        // A card first made from a spawn event does not know its node, and an
        // agent without one is judged as local.
        store.addOrUpdateAgent(agent({ id: "id-1", name: "worker" }));
        store.onHeartbeat(hb({ node: "rpi" }));
        expect(store.getAgents()[0]?.node).toBe("rpi");
    });

    it("moves an agent home when its heartbeat says it runs here, and says so", () => {
        // "" is how the server's own agents report where they run.
        store.addOrUpdateAgent(agent({ node: "edge" }));
        dash.updateAgent.mockClear();

        store.onHeartbeat(hb({ node: "" }));

        expect(store.getAgents()[0]?.node).toBe("");
        expect(dash.updateAgent).toHaveBeenCalledOnce();
    });

    it("does not redraw for a heartbeat from where it already runs", () => {
        store.addOrUpdateAgent(agent({ node: "edge" }));
        dash.updateAgent.mockClear();

        store.onHeartbeat(hb({ node: "edge" }));
        store.onHeartbeat(hb({}));

        expect(dash.updateAgent).not.toHaveBeenCalled();
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
