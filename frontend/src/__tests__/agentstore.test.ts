/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AgentInfo, HeartbeatPayload, AlertPayload, SpawnPayload } from "../types/agent";
import { AgentStore, type AgentChanges } from "../agents/AgentStore";
import { NODE_EVICT_MS, STALE_MS } from "../agents/liveness";
import { settled } from "./dashboard";

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
        .list()
        .map(a => a.id)
        .sort();

let store: AgentStore;
let told: AgentChanges[];

/** What the store told its listener since the last call, once it has. */
async function changes(): Promise<AgentChanges[]> {
    await settled();
    const seen = told;
    told = [];
    return seen;
}

beforeEach(() => {
    store = new AgentStore();
    told = [];
    store.subscribe(c => told.push(c));
});
afterEach(() => {
    vi.restoreAllMocks();
});

describe("AgentStore — addOrUpdateAgent", () => {
    it("adds a new agent and says so", async () => {
        store.addOrUpdateAgent(agent());
        const [batch] = await changes();
        expect([...batch!.added]).toEqual(["id-1"]);
        expect(store.list()).toHaveLength(1);
    });

    it("merges an existing agent (same id) and says it was updated", async () => {
        store.addOrUpdateAgent(agent({ cpu: 1 }));
        await changes();
        store.addOrUpdateAgent(agent({ task: "x" }));
        const [batch] = await changes();
        expect([...batch!.updated]).toEqual(["id-1"]);
        expect(store.get("id-1")).toMatchObject({ cpu: 1, task: "x" });
    });

    it("keeps two agents of one name: a second is not a restart of the first", () => {
        store.addOrUpdateAgent(agent({ id: "on-main" }));
        store.addOrUpdateAgent(agent({ id: "on-pi", node: "pi" }));
        expect(ids()).toEqual(["on-main", "on-pi"]);
        expect(store.byName("worker").map(a => a.id)).toEqual(["on-main", "on-pi"]);
    });

    it("keeps protected and node sticky across a partial update", () => {
        store.addOrUpdateAgent(agent({ protected: true, node: "n1" }));
        store.addOrUpdateAgent(agent({ protected: false }));
        expect(store.get("id-1")).toMatchObject({ protected: true, node: "n1" });
    });

    it("replaces a record rather than changing the one a reader holds", () => {
        store.addOrUpdateAgent(agent({ state: "running" }));
        const held = store.get("id-1")!;
        store.addOrUpdateAgent(agent({ state: "stopped" }));
        expect(held.state).toBe("running");
        expect(store.get("id-1")!.state).toBe("stopped");
    });

    it("tells a whole batch at once, after the code that made it", async () => {
        store.addOrUpdateAgent(agent({ id: "a" }));
        store.addOrUpdateAgent(agent({ id: "b" }));
        expect(told).toEqual([]);
        const batches = await changes();
        expect(batches).toHaveLength(1);
        expect([...batches[0]!.added]).toEqual(["a", "b"]);
    });

    it("stops telling a listener that unsubscribed", async () => {
        const quiet = vi.fn();
        store.subscribe(quiet)();
        store.addOrUpdateAgent(agent());
        await settled();
        expect(quiet).not.toHaveBeenCalled();
    });
});

describe("AgentStore — labels", () => {
    it("calls an agent by its name while it is the only one", () => {
        store.addOrUpdateAgent(agent({ node: "pi" }));
        expect(store.label(store.get("id-1")!)).toBe("worker");
    });

    it("adds the node once a name is shared, main for one running here", () => {
        store.addOrUpdateAgent(agent({ id: "here" }));
        store.addOrUpdateAgent(agent({ id: "there", node: "pi" }));
        expect(store.label(store.get("here")!)).toBe("worker@main");
        expect(store.label(store.get("there")!)).toBe("worker@pi");
    });
});

describe("AgentStore — removal", () => {
    it("removeAgent drops it and says which agent left, as it was", async () => {
        store.addOrUpdateAgent(agent());
        await changes();
        store.removeAgent("id-1");
        const [batch] = await changes();
        expect(batch!.removed.get("id-1")).toMatchObject({ name: "worker" });
        expect(store.list()).toEqual([]);
    });

    it("says nothing of one that came and went within a batch", async () => {
        store.addOrUpdateAgent(agent());
        store.removeAgent("id-1");
        const [batch] = await changes();
        expect(batch!.added.size + batch!.removed.size).toBe(0);
    });

    it("counts one gone and back within a batch as changed, not gone", async () => {
        store.addOrUpdateAgent(agent());
        await changes();
        store.clearAll();
        store.addOrUpdateAgent(agent());
        const [batch] = await changes();
        expect(batch!.removed.size).toBe(0);
        expect([...batch!.updated]).toEqual(["id-1"]);
    });

    it("ignores the removal of an agent it does not hold", async () => {
        store.removeAgent("ghost");
        expect(await changes()).toEqual([]);
    });

    it("clearAll removes every agent", () => {
        store.addOrUpdateAgent(agent({ id: "a" }));
        store.addOrUpdateAgent(agent({ id: "b", name: "other" }));
        store.clearAll();
        expect(store.list()).toEqual([]);
    });
});

describe("AgentStore — remote nodes", () => {
    it("records a node's list and readings, and says where agents run may have changed", async () => {
        store.updateRemoteNode("n1", ["someone-else"], { cpu_pct: 12 });
        expect(store.nodes.get("n1")).toMatchObject({ agents: ["someone-else"], readings: { cpu_pct: 12 } });
        const [batch] = await changes();
        expect(batch!.placement).toBe(true);
    });

    it("keeps a node's agent that one heartbeat did not list", () => {
        // Restarting, or listed under a slightly different name: not gone. An
        // agent that is gone is deleted by the server.
        store.addOrUpdateAgent(agent({ id: "r1", name: "remote-1", node: "n1" }));
        store.updateRemoteNode("n1", ["someone-else"]);
        store.updateRemoteNode("n1", []);
        expect(ids()).toEqual(["r1"]);
    });

    it("keeps the manifest a heartbeat does not carry", () => {
        store.mergeNodeListings([{ node: "edge-1", online: true, manifest: { arch: "aarch64" } }]);
        store.updateRemoteNode("edge-1", ["alpha"], { cpu_pct: 9 });
        expect(store.nodes.get("edge-1")).toMatchObject({
            agents: ["alpha"],
            readings: { cpu_pct: 9 },
            manifest: { arch: "aarch64" },
        });
    });

    it("keeps a node that has merely gone quiet, so it can render as offline", () => {
        store.updateRemoteNode("edge-1", ["alpha"], undefined, 1_000);
        store.updateRemoteNode("edge-2", ["beta"], undefined, 1_000 + STALE_MS * 2);
        expect(store.nodes.has("edge-1")).toBe(true);
    });

    it("drops a node that has been gone far longer than offline, never the one reporting", () => {
        store.updateRemoteNode("edge-1", ["alpha"], undefined, 1_000);
        store.updateRemoteNode("edge-2", ["beta"], undefined, 1_000 + NODE_EVICT_MS + 1);
        expect([...store.nodes.keys()]).toEqual(["edge-2"]);
        store.updateRemoteNode("edge-2", ["beta", "gamma"], undefined, 1_000 + NODE_EVICT_MS * 3);
        expect(store.nodes.get("edge-2")?.agents).toEqual(["beta", "gamma"]);
    });

    it("takes manifests from a listing, and holds a listed node not heard from yet", () => {
        store.updateRemoteNode("edge-1", ["alpha"]);
        store.mergeNodeListings([
            { node: "edge-1", online: true, manifest: { arch: "x86_64" } },
            {
                node: "rpi",
                online: true,
                last_seen: 12,
                agents: ["flic", 3],
                cpu_pct: 12,
                manifest: { arch: "aarch64" },
            },
            { node: "bare", online: false },
        ]);
        expect(store.nodes.get("edge-1")?.manifest).toEqual({ arch: "x86_64" });
        expect(store.nodes.get("rpi")).toMatchObject({
            agents: ["flic"],
            lastSeen: 12_000,
            readings: { cpu_pct: 12 },
            manifest: { arch: "aarch64" },
        });
        expect(store.nodes.get("bare")).toMatchObject({ agents: [], lastSeen: 0, manifest: null });
    });

    it("says nothing for a listing that tells it nothing new", async () => {
        store.mergeNodeListings([{ node: "rpi", online: true, manifest: { arch: "aarch64" } }]);
        await changes();
        store.mergeNodeListings([{ node: "rpi", online: true, manifest: { arch: "aarch64" } }]);
        expect(await changes()).toEqual([]);
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
    });

    it("lets go of an unlisted local agent once it has also been silent past the stale window", async () => {
        // For the delete frame that never arrived.
        const now = 1_000_000;
        vi.spyOn(Date, "now").mockReturnValue(now);
        store.addOrUpdateAgent(agent({ id: "gone", name: "gone" }));
        store.addOrUpdateAgent(agent({ id: "remote", name: "remote", node: "n1" }));
        await changes();

        store.reconcileAgents([agent({ id: "kept", name: "kept" })], now + STALE_MS + 1);

        expect(ids()).toEqual(["kept", "remote"]); // a node's agent is never in the list
        const [batch] = await changes();
        expect([...batch!.removed.keys()]).toEqual(["gone"]);
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
});

describe("AgentStore — heartbeat", () => {
    it("updates an existing agent's live fields and pulses its card", async () => {
        store.addOrUpdateAgent(agent());
        await changes();
        store.onHeartbeat(hb({ state: "stopped", cpu: 12, memory_mb: 256, task: "busy" }));
        expect(store.get("id-1")).toMatchObject({ state: "stopped", cpu: 12, mem: 256, task: "busy" });
        const [batch] = await changes();
        expect([...batch!.updated]).toEqual(["id-1"]);
        expect(batch!.activity).toEqual([{ kind: "heartbeat", id: "id-1", timestampMs: 1_000 }]);
    });

    it("learns where an existing agent runs from a later heartbeat", () => {
        // A card first made from a spawn event does not know its node, and an
        // agent without one is judged as local.
        store.addOrUpdateAgent(agent({ id: "id-1", name: "worker" }));
        store.onHeartbeat(hb({ node: "rpi" }));
        expect(store.get("id-1")?.node).toBe("rpi");
    });

    it("moves an agent home when its heartbeat says it runs here, and says so", async () => {
        // "" is how the server's own agents report where they run.
        store.addOrUpdateAgent(agent({ node: "edge" }));
        await changes();

        store.onHeartbeat(hb({ node: "" }));

        expect(store.get("id-1")?.node).toBe("");
        const [batch] = await changes();
        expect(batch!.placement).toBe(true);
        expect([...batch!.updated]).toEqual(["id-1"]);
    });

    it("only pulses for a heartbeat that changes nothing shown", async () => {
        store.addOrUpdateAgent(agent({ node: "edge" }));
        await changes();

        store.onHeartbeat(hb({ node: "edge" }));
        store.onHeartbeat(hb({}));

        const [batch] = await changes();
        expect(batch!.updated.size).toBe(0);
        expect(batch!.placement).toBe(false);
        expect(batch!.activity).toHaveLength(2);
    });

    it("creates an unknown agent and still pulses its card", async () => {
        store.onHeartbeat(hb({ agentId: "new", agentName: "newbie", node: "n2" }));
        expect(ids()).toEqual(["new"]);
        expect(store.get("new")).toMatchObject({ node: "n2" });
        const [batch] = await changes();
        expect([...batch!.added]).toEqual(["new"]);
        expect(batch!.activity).toEqual([{ kind: "heartbeat", id: "new", timestampMs: 1_000 }]);
    });

    it("falls back to Date.now() when the heartbeat timestamp is non-finite", () => {
        vi.spyOn(Date, "now").mockReturnValue(5_000);
        store.onHeartbeat(hb({ agentId: "n", agentName: "n", timestampMs: NaN }));
        expect(store.get("n")?.lastHeartbeatAt).toBe(new Date(5_000).toISOString());
    });
});

describe("AgentStore — alert / chat / spawn", () => {
    it("onAlert shows on the agent's card", async () => {
        const payload: AlertPayload = {
            agentId: "id-1",
            agentName: "worker",
            severity: "error",
            message: "boom",
            timestampMs: 1,
        };
        store.onAlert(payload);
        const [batch] = await changes();
        expect(batch!.activity).toEqual([{ kind: "alert", id: "id-1", severity: "error" }]);
    });

    it("onChat finds the sender by name", async () => {
        store.addOrUpdateAgent(agent({ id: "a", name: "alice" }));
        store.onChat("alice");
        const [batch] = await changes();
        expect(batch!.activity).toEqual([{ kind: "chat", id: "a" }]);
    });

    it("onChat shows nothing for a name it does not hold, or one that several agents share", async () => {
        store.addOrUpdateAgent(agent({ id: "a", name: "alice" }));
        store.addOrUpdateAgent(agent({ id: "a2", name: "alice", node: "pi" }));
        await changes();
        store.onChat("ghost");
        store.onChat("alice");
        expect(await changes()).toEqual([]);
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
        expect(store.get("s1")).toMatchObject({
            id: "s1",
            name: "spawned",
            state: "initializing",
            protected: true,
            agentType: "dynamic",
        });
    });
});
