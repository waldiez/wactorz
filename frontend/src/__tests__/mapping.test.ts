/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { buildNameIndex, mapLogFeedItem, toAgentInfo } from "../agents/mapping";
import { resolveAgentName } from "../agents/naming";
import type { LogFeedItem, StatePatchAgent } from "../io/WSChatClient";

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
