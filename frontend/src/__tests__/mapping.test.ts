/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { buildNameIndex, mapLogFeedItem } from "../agents/mapping";
import type { LogFeedItem } from "../io/WSChatClient";

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
