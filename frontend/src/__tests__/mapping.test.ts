/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import {
    buildNameIndex,
    mapLogFeedItem,
    toAgentInfo,
    buildMetricsUpdate,
    heartbeatFeedItem,
    spawnTypeLabel,
    spawnFeedItem,
    alertFeedType,
    alertToastType,
    alertFeedItem,
    chatFeedItem,
    stoppedFeedItem,
    qaFlagFeedItem,
    logMessage,
    logFeedItem,
    completedFeedItem,
    nodeHeartbeatFeedItem,
} from "../agents/mapping";
import { resolveAgentName } from "../agents/naming";
import type { LogFeedItem, StatePatchAgent } from "../types/ws";
import type {
    HeartbeatPayload,
    SpawnPayload,
    AlertPayload,
    StatusPayload,
    LogPayload,
    NodeHeartbeatPayload,
    ChatMessage,
    MetricsPayload,
    AgentInfo,
    QaFlagPayload,
} from "../types/agent";

describe("toAgentInfo", () => {
    it("maps required fields with sensible defaults", () => {
        const out = toAgentInfo({ agent_id: "main-0001", name: "main" } as StatePatchAgent);
        expect(out).toMatchObject({
            id: "main-0001",
            name: resolveAgentName("main", "main-0001"),
            state: "running", // default when neither state nor status given
            protected: false,
        });
    });

    it("passes through known states and falls back to running for unknown", () => {
        expect(toAgentInfo({ agent_id: "a", state: "paused" } as StatePatchAgent).state).toBe("paused");
        expect(toAgentInfo({ agent_id: "a", state: "weird" } as StatePatchAgent).state).toBe("running");
    });

    it("uses status when state is absent", () => {
        expect(toAgentInfo({ agent_id: "a", status: "stopped" } as StatePatchAgent).state).toBe("stopped");
    });

    it("copies optional metrics through to camelCase fields when present", () => {
        const out = toAgentInfo({
            agent_id: "a",
            name: "n",
            protected: true,
            messages_processed: 7,
            cost_usd: 1.5,
            uptime: 99,
            cpu: 12,
            mem: 34,
            task: "busy",
            agent_type: "dynamic",
        } as StatePatchAgent);
        expect(out).toMatchObject({
            protected: true,
            messagesProcessed: 7,
            costUsd: 1.5,
            uptime: 99,
            cpu: 12,
            mem: 34,
            task: "busy",
            agentType: "dynamic",
        });
    });

    it("omits optional fields that are not supplied", () => {
        const out = toAgentInfo({ agent_id: "a" } as StatePatchAgent);
        expect(out.messagesProcessed).toBeUndefined();
        expect(out.costUsd).toBeUndefined();
        expect(out.agentType).toBeUndefined();
    });
});

describe("buildNameIndex", () => {
    it("indexes entries that carry a name by agent_id", () => {
        const items: LogFeedItem[] = [
            { type: "spawned", agent_id: "uuid-1", name: "Living Room Temp" },
            { type: "log", agent_id: "uuid-1", message: "27.3°C" },
        ];
        const index = buildNameIndex(items);
        expect(index.get("uuid-1")).toBe("Living Room Temp");
    });

    it("skips entries without an id or a name", () => {
        const items: LogFeedItem[] = [
            { type: "log", message: "no id" },
            { type: "log", agent_id: "uuid-2", message: "no name" },
        ];
        const index = buildNameIndex(items);
        expect(index.size).toBe(0);
    });

    it("prefers agentName when name is absent", () => {
        const items: LogFeedItem[] = [{ type: "spawned", agent_id: "uuid-3", agentName: "Kitchen" }];
        expect(buildNameIndex(items).get("uuid-3")).toBe("Kitchen");
    });
});

describe("mapLogFeedItem", () => {
    it("attributes a nameless log entry via the resolver", () => {
        const item: LogFeedItem = { type: "log", agent_id: "uuid-1", message: "27.3°C", timestamp: 1 };
        const mapped = mapLogFeedItem(item, id => (id === "uuid-1" ? "Living Room Temp" : undefined));
        expect(mapped).toEqual({
            type: "chat",
            label: "27.3°C",
            agentName: "Living Room Temp",
            timestamp: 1000,
        });
    });

    it("falls back to the id-derived name when the resolver yields nothing", () => {
        const item: LogFeedItem = { type: "log", agent_id: "256e69fb-04f4", message: "hi" };
        const mapped = mapLogFeedItem(item);
        expect(mapped?.agentName).toBe("256e69fb-04f4");
    });

    it("prefers an explicit name on the item over the resolver", () => {
        const item: LogFeedItem = { type: "log", agent_id: "uuid-1", name: "Explicit", message: "hi" };
        const mapped = mapLogFeedItem(item, () => "From Resolver");
        expect(mapped?.agentName).toBe("Explicit");
    });

    it("drops a log entry with no message", () => {
        expect(mapLogFeedItem({ type: "log", agent_id: "uuid-1" })).toBeNull();
    });

    it("returns null for an unknown type", () => {
        expect(mapLogFeedItem({ type: "mystery", agent_id: "x" })).toBeNull();
    });
});

describe("buildMetricsUpdate", () => {
    const existing: AgentInfo = { id: "id1", name: "Agent One", state: "running", protected: true };

    it("preserves identity/state and copies only present fields", () => {
        const p = { agentId: "id1", costUsd: 1.5, messagesProcessed: 4 } as MetricsPayload;
        expect(buildMetricsUpdate(p, existing)).toEqual({
            id: "id1",
            name: "Agent One",
            state: "running",
            protected: true,
            costUsd: 1.5,
            messagesProcessed: 4,
        });
    });

    it("omits absent optional fields entirely (no undefined keys)", () => {
        const update = buildMetricsUpdate({ agentId: "id1" } as MetricsPayload, existing);
        expect("costUsd" in update).toBe(false);
        expect("uptime" in update).toBe(false);
        expect("messagesProcessed" in update).toBe(false);
    });
});

describe("live MQTT event feed builders", () => {
    it("heartbeatFeedItem maps to a heartbeat row at the payload timestamp", () => {
        const p = { agentName: "a1", timestampMs: 100 } as HeartbeatPayload;
        expect(heartbeatFeedItem(p)).toEqual({
            type: "heartbeat",
            label: "heartbeat",
            agentName: "a1",
            timestamp: 100,
        });
    });

    it("spawn uses the agent type, falling back to 'agent' on empty string", () => {
        expect(spawnTypeLabel({ agentType: "worker" } as SpawnPayload)).toBe("worker");
        expect(spawnTypeLabel({ agentType: "" } as SpawnPayload)).toBe("agent");
        const p = { agentName: "a", agentType: "", timestampMs: 5 } as SpawnPayload;
        expect(spawnFeedItem(p)).toEqual({
            type: "spawn",
            label: "spawned (agent)",
            agentName: "a",
            timestamp: 5,
        });
    });

    it("alert feed kind: only error → error; critical/warning/info → warning", () => {
        expect(alertFeedType("error")).toBe("alert-error");
        expect(alertFeedType("critical")).toBe("alert-warning");
        expect(alertFeedType("warning")).toBe("alert-warning");
        expect(alertFeedType("info")).toBe("alert-warning");
    });

    it("alert toast kind: error AND critical → error; warning/info → warning", () => {
        expect(alertToastType("error")).toBe("alert-error");
        expect(alertToastType("critical")).toBe("alert-error");
        expect(alertToastType("warning")).toBe("alert-warning");
        expect(alertToastType("info")).toBe("alert-warning");
    });

    it("alertFeedItem builds a row, defaulting a missing message/agent", () => {
        const p = { severity: "error", message: "boom", agentName: "mon", timestampMs: 9 } as AlertPayload;
        expect(alertFeedItem(p)).toEqual({
            type: "alert-error",
            label: "boom",
            agentName: "mon",
            timestamp: 9,
        });
        const bare = { severity: "warning", timestampMs: 3 } as unknown as AlertPayload;
        expect(alertFeedItem(bare)).toEqual({
            type: "alert-warning",
            label: "",
            agentName: "system",
            timestamp: 3,
        });
    });

    it("chatFeedItem formats the routed-to label", () => {
        const msg = { from: "a1", to: "user", content: "hi", timestampMs: 7 } as ChatMessage;
        expect(chatFeedItem(msg)).toEqual({
            type: "chat",
            label: "→ user: hi",
            agentName: "a1",
            timestamp: 7,
        });
    });

    it("stoppedFeedItem stamps the provided time", () => {
        expect(stoppedFeedItem({ agentName: "a" } as StatusPayload, 42)).toEqual({
            type: "stopped",
            label: "stopped",
            agentName: "a",
            timestamp: 42,
        });
    });

    it("qaFlagFeedItem formats category/excerpt and attributes to the qa-agent", () => {
        const p = { category: "safety", excerpt: "bad", from: "writer", timestampMs: 11 } as QaFlagPayload;
        expect(qaFlagFeedItem(p)).toEqual({
            type: "qa-flag",
            label: "[safety] bad",
            agentName: "qa-agent ← writer",
            timestamp: 11,
        });
    });

    it("logMessage prefers message, then text, then empty", () => {
        expect(logMessage({ message: "m", text: "t" } as LogPayload)).toBe("m");
        expect(logMessage({ text: "t" } as LogPayload)).toBe("t");
        expect(logMessage({} as LogPayload)).toBe("");
    });

    it("logFeedItem returns an item only for non-empty messages", () => {
        expect(logFeedItem({ agentName: "a", message: "hello" } as LogPayload, 50)).toEqual({
            type: "chat",
            label: "hello",
            agentName: "a",
            timestamp: 50,
        });
        expect(logFeedItem({ agentName: "a" } as LogPayload)).toBeNull();
    });

    it("completedFeedItem renders a task-completed row", () => {
        expect(completedFeedItem({ agentName: "a" }, 8)).toEqual({
            type: "spawn",
            label: "task completed",
            agentName: "a",
            timestamp: 8,
        });
    });

    it("nodeHeartbeatFeedItem pluralises the agent count", () => {
        expect(nodeHeartbeatFeedItem({ node: "n", agents: ["x"] } as NodeHeartbeatPayload, 1).label).toBe(
            "node online · 1 agent",
        );
        expect(
            nodeHeartbeatFeedItem({ node: "n", agents: ["x", "y"] } as NodeHeartbeatPayload, 1).label,
        ).toBe("node online · 2 agents");
        expect(nodeHeartbeatFeedItem({ node: "n", agents: [] } as NodeHeartbeatPayload, 1).label).toBe(
            "node online · 0 agents",
        );
    });
});
