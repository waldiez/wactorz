/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import {
    ServerEventRouter,
    normaliseHeartbeat,
    normaliseChat,
    normaliseStatus,
    normaliseSpawn,
    normaliseAlert,
    normaliseQaFlag,
} from "../io/ServerEventRouter";
import { resolveAgentName } from "../agents/naming";

describe("normaliseHeartbeat", () => {
    it("accepts camelCase payload", () => {
        const hb = normaliseHeartbeat({
            agentId: "20260325T151725.0000Z-alpha",
            agentName: "alpha",
            state: "running",
            sequence: 5,
            timestampMs: 1_700_000_000_000,
        });
        expect(hb.agentId).toBe("20260325T151725.0000Z-alpha");
        expect(hb.agentName).toBe("alpha");
        expect(hb.sequence).toBe(5);
        expect(hb.timestampMs).toBe(1_700_000_000_000);
    });

    it("accepts snake_case Python payload and converts timestamp", () => {
        const hb = normaliseHeartbeat({
            actor_id: "20260325T151725.0000Z-bravo",
            name: "bravo",
            state: "running",
            sequence: 1,
            timestamp: 1_700_000_000, // seconds
        });
        expect(hb.agentId).toBe("20260325T151725.0000Z-bravo");
        expect(hb.agentName).toBe("bravo");
        expect(hb.timestampMs).toBe(1_700_000_000_000);
    });

    it("falls back sequence to 0 when missing", () => {
        const hb = normaliseHeartbeat({ agentId: "x", agentName: "x", state: "running" });
        expect(hb.sequence).toBe(0);
    });

    it("resolves name from WID id when name is absent", () => {
        const hb = normaliseHeartbeat({ agentId: "20260325T151725.0000Z-delta", state: "running" });
        expect(hb.agentName).toBe("delta");
    });

    it("handles empty payload gracefully", () => {
        const hb = normaliseHeartbeat({});
        expect(hb.agentId).toBe("");
        expect(hb.sequence).toBe(0);
    });

    it("handles null payload", () => {
        const hb = normaliseHeartbeat(null);
        expect(hb.agentId).toBe("");
    });
});

describe("normaliseChat", () => {
    it("passes through well-formed chat message", () => {
        const msg = normaliseChat({
            id: "msg-1",
            from: "main-actor",
            to: "user",
            content: "Hello",
            timestampMs: 1_700_000_000_000,
        });
        expect(msg.id).toBe("msg-1");
        expect(msg.from).toBe("main-actor");
        expect(msg.to).toBe("user");
        expect(msg.content).toBe("Hello");
    });

    it("generates id when absent", () => {
        const msg = normaliseChat({ content: "Hi", timestampMs: 1_700_000_000_000 });
        expect(msg.id).toMatch(/^chat-/);
    });

    it("generates unique ids for same-millisecond messages", () => {
        const ts = 1_700_000_000_000;
        const a = normaliseChat({ content: "one", timestampMs: ts });
        const b = normaliseChat({ content: "two", timestampMs: ts });
        expect(a.id).not.toBe(b.id);
        expect(a.timestampMs).toBe(ts); // timestamp still carried as its own field
    });

    it("defaults to field from agentName when from is absent", () => {
        const msg = normaliseChat({ agentName: "io-agent", content: "hi" });
        expect(msg.from).toBe("io-agent");
    });

    it("defaults to 'user' when to is absent", () => {
        const msg = normaliseChat({ from: "agent", content: "hi" });
        expect(msg.to).toBe("user");
    });

    it("converts Python second timestamp", () => {
        const msg = normaliseChat({ timestamp: 1_700_000_000 });
        expect(msg.timestampMs).toBe(1_700_000_000_000);
    });

    it("handles null payload", () => {
        const msg = normaliseChat(null);
        expect(msg.content).toBe("");
        expect(msg.to).toBe("user");
    });
});

describe("normaliseStatus", () => {
    it("maps camelCase fields", () => {
        const s = normaliseStatus({
            agentId: "20260325T151725.0000Z-alpha",
            agentName: "alpha",
            state: "running",
            messagesReceived: 10,
            messagesProcessed: 8,
            messagesFailed: 2,
        });
        expect(s.agentId).toBe("20260325T151725.0000Z-alpha");
        expect(s.agentName).toBe("alpha");
    });

    it("maps snake_case actor_id", () => {
        const s = normaliseStatus({ actor_id: "20260325T151725.0000Z-bravo", name: "bravo" });
        expect(s.agentId).toBe("20260325T151725.0000Z-bravo");
        expect(s.agentName).toBe("bravo");
    });
});

// The normalisers turn untrusted external JSON into typed payloads. They must
// validate field types (not blindly spread the raw object), so a hostile or
// malformed value can't be laundered in as if it were validated.
describe("normaliser field validation (hardening)", () => {
    it("drops a non-numeric cpu instead of trusting it (would crash toFixed downstream)", () => {
        const hb = normaliseHeartbeat({ agentId: "x", cpu: "evil", memory_mb: "nope" });
        expect(hb.cpu).toBeUndefined();
        expect(hb.memory_mb).toBeUndefined();
    });

    it("does not launder unknown keys into the typed payload", () => {
        const hb = normaliseHeartbeat({ agentId: "x", state: "running", __proto__hack: 1, evil: "y" });
        expect("evil" in hb).toBe(false);
        expect("__proto__hack" in hb).toBe(false);
    });

    it("coerces an invalid state to 'running'", () => {
        expect(normaliseHeartbeat({ agentId: "x", state: "garbage" }).state).toBe("running");
        expect(normaliseHeartbeat({ agentId: "x", state: { failed: "boom" } }).state).toEqual({
            failed: "boom",
        });
    });

    it("coerces an unknown alert severity to 'info'", () => {
        expect(normaliseAlert({ agentId: "x", severity: "nuclear", message: "m" }).severity).toBe("info");
    });

    it("supports snake_case agent_type and yields '' (not undefined) when absent", () => {
        expect(normaliseSpawn({ agentId: "x", agent_type: "monitor" }).agentType).toBe("monitor");
        expect(normaliseSpawn({ agentId: "x" }).agentType).toBe("");
    });

    it("normalises a qa-flag: resolves snake_case id, coerces fields, defaults missing to ''", () => {
        const qf = normaliseQaFlag({
            agent_id: "qa-1",
            from: "writer",
            category: "safety",
            severity: "high",
            excerpt: "bad output",
            message: "flagged",
            timestamp: 1_700_000_000,
        });
        expect(qf).toEqual({
            agentId: "qa-1",
            agentName: resolveAgentName("", "qa-1"),
            from: "writer",
            category: "safety",
            severity: "high",
            excerpt: "bad output",
            message: "flagged",
            timestampMs: 1_700_000_000_000, // seconds → ms
        });
        // Non-string / absent fields don't leak through.
        const bare = normaliseQaFlag({ category: 42 });
        expect(bare.category).toBe("");
        expect(bare.excerpt).toBe("");
        expect(bare.from).toBe("");
    });

    it("does not throw on a null payload", () => {
        expect(() => normaliseHeartbeat(null)).not.toThrow();
        expect(() => normaliseStatus(null)).not.toThrow();
        expect(() => normaliseAlert(null)).not.toThrow();
        expect(() => normaliseQaFlag(null)).not.toThrow();
    });
});

describe("ServerEventRouter.route — topic dispatch", () => {
    // Register a listener for `event` and return the array it records into.
    function capture(r: ServerEventRouter, event: Parameters<ServerEventRouter["on"]>[0]) {
        const calls: unknown[] = [];
        r.on(event, (d: unknown) => calls.push(d));
        return calls;
    }

    it("routes fixed-shape agent topics to their typed events", () => {
        const r = new ServerEventRouter();
        const hb = capture(r, "heartbeat");
        const st = capture(r, "status");
        const al = capture(r, "alert");
        const ch = capture(r, "chat");
        const sp = capture(r, "spawn");
        r.route("agents/a1/heartbeat", { agentId: "a1", name: "alpha", state: "running" });
        r.route("agents/a1/status", { agentId: "a1", name: "alpha", state: "running" });
        r.route("agents/a1/alert", { agentId: "a1", severity: "error", message: "m" });
        r.route("agents/a1/chat", { content: "hi", from: "alpha" });
        r.route("agents/a1/spawn", { agentId: "a1", agent_type: "worker" });
        expect((hb[0] as { agentId: string }).agentId).toBe("a1");
        expect((st[0] as { agentName: string }).agentName).toBe("alpha");
        expect((al[0] as { severity: string }).severity).toBe("error");
        expect((ch[0] as { content: string }).content).toBe("hi");
        expect((sp[0] as { agentType: string }).agentType).toBe("worker");
    });

    it("routes exact system topics", () => {
        const r = new ServerEventRouter();
        const qa = capture(r, "qa-flag");
        const legacySpawn = capture(r, "spawn");
        const health = capture(r, "system-health");
        r.route("system/qa-flag", { agent_id: "q", category: "safety" });
        r.route("system/spawn", { agentId: "s", agent_type: "worker" }); // legacy spawn topic
        r.route("system/health", { ok: true });
        expect((qa[0] as { category: string }).category).toBe("safety");
        expect((legacySpawn[0] as { agentType: string }).agentType).toBe("worker");
        expect(health[0]).toEqual({ ok: true }); // raw passthrough
    });

    it("routes system/host to host-stats (snake_case and camelCase)", () => {
        const r = new ServerEventRouter();
        const hs = capture(r, "host-stats");
        r.route("system/host", { cpu: 1, mem_used_mb: 2, mem_total_mb: 3 });
        r.route("system/host", { cpu_pct: 9, memUsedMb: 4, memTotalMb: 5 });
        r.route("system/host", { cpu: "x", mem_used_mb: null }); // non-numeric → dropped
        expect(hs[0]).toEqual({ cpu: 1, memUsedMb: 2, memTotalMb: 3 });
        expect(hs[1]).toEqual({ cpu: 9, memUsedMb: 4, memTotalMb: 5 });
        expect(hs[2]).toEqual({});
    });

    it("routes parameterised agent topics (metrics/logs/completed)", () => {
        const r = new ServerEventRouter();
        const metrics = capture(r, "metrics");
        const logs = capture(r, "logs");
        const completed = capture(r, "completed");
        r.route("agents/a1/metrics", {
            name: "alpha",
            cost_usd: 0.5,
            input_tokens: 10,
            output_tokens: 20,
            messages_processed: 3,
            uptime: 99,
        });
        r.route("agents/a1/logs", { name: "alpha", message: "hello" });
        r.route("agents/a1/logs", { name: "alpha" }); // no message → omitted
        r.route("agents/a1/completed", { name: "alpha" });
        expect(metrics[0]).toEqual({
            agentId: "a1",
            agentName: "alpha",
            costUsd: 0.5,
            inputTokens: 10,
            outputTokens: 20,
            messagesProcessed: 3,
            uptime: 99,
        });
        expect(logs[0]).toEqual({ agentId: "a1", agentName: "alpha", message: "hello" });
        expect(logs[1]).toEqual({ agentId: "a1", agentName: "alpha" }); // no `message` key
        expect(completed[0]).toEqual({ agentId: "a1", agentName: "alpha" });
    });

    it("routes nodes/{id}/heartbeat to node-heartbeat (with and without node_id)", () => {
        const r = new ServerEventRouter();
        const node = capture(r, "node-heartbeat");
        r.route("nodes/edge-1/heartbeat", { agents: ["a", "b"], node_id: "n1" });
        r.route("nodes/edge-2/heartbeat", { agents: ["c"] }); // no node_id → omitted
        expect(node[0]).toEqual({ node: "edge-1", agents: ["a", "b"], nodeId: "n1" });
        expect(node[1]).toEqual({ node: "edge-2", agents: ["c"] });
    });

    it("falls back to the id prefix when no name is in the payload", () => {
        const r = new ServerEventRouter();
        const logs = capture(r, "logs");
        r.route("agents/abcdefghijkl/logs", { message: "x" });
        expect((logs[0] as { agentName: string }).agentName).toBe("abcdefgh");
    });

    it("surfaces unmatched topics and non-object payloads as `raw`", () => {
        const r = new ServerEventRouter();
        const raw = capture(r, "raw");
        r.route("homeassistant/state_changes/light/x", { new_state: {} }); // unknown → raw
        r.route("agents/a1/whatever", { x: 1 }); // unknown agent metric → raw
        r.route("agents/a1/heartbeat", "not-an-object"); // primitive payload → raw
        r.route("agents/a1/heartbeat", null); // null payload → raw
        expect(raw).toHaveLength(4);
        expect(raw[0]).toEqual({ topic: "homeassistant/state_changes/light/x", payload: { new_state: {} } });
        expect(raw[2]).toEqual({ topic: "agents/a1/heartbeat", payload: "not-an-object" });
        expect(raw[3]).toEqual({ topic: "agents/a1/heartbeat", payload: null });
    });

    it("on() registers, off() removes, and multiple listeners all fire", () => {
        const r = new ServerEventRouter();
        const a = vi.fn();
        const b = vi.fn();
        r.on("completed", a).on("completed", b);
        r.route("agents/x/completed", {});
        expect(a).toHaveBeenCalledTimes(1);
        expect(b).toHaveBeenCalledTimes(1);
        r.off("completed", a);
        r.route("agents/x/completed", {});
        expect(a).toHaveBeenCalledTimes(1); // not called again
        expect(b).toHaveBeenCalledTimes(2);
        // off() for an unregistered listener is a no-op (covers the guard branch).
        expect(() => r.off("heartbeat", vi.fn())).not.toThrow();
    });

    it("a throwing listener is caught and does not block the others", () => {
        const r = new ServerEventRouter();
        const good = vi.fn();
        r.on("completed", () => {
            throw new Error("boom");
        });
        r.on("completed", good);
        expect(() => r.route("agents/x/completed", {})).not.toThrow();
        expect(good).toHaveBeenCalledTimes(1);
    });
});
