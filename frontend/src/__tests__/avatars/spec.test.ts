/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** What an agent says of its avatar, on its way from the server to the store. */
import { describe, expect, it } from "vitest";

import { AgentStore } from "../../agents/AgentStore";
import { avatarField, readAvatarSpec, sameAvatar } from "../../agents/avatarSpec";
import { reconcileActorList, toAgentInfo } from "../../agents/mapping";
import { normaliseHeartbeat } from "../../io/ServerEventRouter";
import type { AgentInfo } from "../../types/agent";

describe("an avatar as it arrives", () => {
    it("keeps its parts that are text or a number", () => {
        expect(readAvatarSpec({ kind: "flic", ant: 2, top: "" })).toEqual({ kind: "flic", ant: 2, top: "" });
        expect(
            readAvatarSpec({ kind: "flic", nested: { a: 1 }, list: ["x"], on: true, none: null, nan: NaN }),
        ).toEqual({ kind: "flic" });
    });

    it("keeps no more parts than a character has", () => {
        const many = Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`part${i}`, "x"]));
        expect(Object.keys(readAvatarSpec(many)!)).toHaveLength(12);
    });

    it("is nothing when the payload does not speak of one", () => {
        for (const raw of [undefined, null, "main", 3, ["main"]]) {
            expect(readAvatarSpec(raw)).toBeUndefined();
            expect(avatarField(raw)).toEqual({});
        }
        expect(avatarField({ kind: "main" })).toEqual({ avatar: { kind: "main" } });
        expect(avatarField({})).toEqual({ avatar: {} });
    });

    it("is the same as another that says the same, absent and empty alike", () => {
        expect(sameAvatar(undefined, {})).toBe(true);
        expect(sameAvatar({ kind: "a" }, { kind: "a" })).toBe(true);
        expect(sameAvatar({ kind: "a" }, { kind: "b" })).toBe(false);
        expect(sameAvatar({ kind: "a" }, undefined)).toBe(false);
    });
});

describe("the avatar on its way to the store", () => {
    it("comes with a state patch", () => {
        expect(toAgentInfo({ agent_id: "a1", name: "x", avatar: { kind: "flic" } }).avatar).toEqual({
            kind: "flic",
        });
        expect("avatar" in toAgentInfo({ agent_id: "a1", name: "x" })).toBe(false);
        expect("avatar" in toAgentInfo({ agent_id: "a1", name: "x", avatar: "flic" })).toBe(false);
    });

    it("comes with the list of actors, checked like any other", () => {
        const listed = [
            { id: "a1", name: "x", state: "running", protected: false, avatar: { kind: "flic", bad: [1] } },
            { id: "a2", name: "y", state: "running", protected: false },
            { id: "a3", name: "z", state: "running", protected: false, avatar: "flic" },
        ] as unknown as AgentInfo[];
        const [first, second, third] = reconcileActorList(listed, () => false);
        expect(first!.avatar).toEqual({ kind: "flic" });
        expect("avatar" in second!).toBe(false);
        expect("avatar" in third!).toBe(false);
    });

    it("comes with a heartbeat", () => {
        expect(normaliseHeartbeat({ actor_id: "a1", name: "x", avatar: { kind: "flic" } }).avatar).toEqual({
            kind: "flic",
        });
        expect("avatar" in normaliseHeartbeat({ actor_id: "a1", name: "x" })).toBe(false);
    });
});

describe("the store", () => {
    function beat(store: AgentStore, avatar?: Record<string, string | number>): void {
        store.onHeartbeat({
            agentId: "a1",
            agentName: "x",
            state: "running",
            sequence: 0,
            timestampMs: Date.now(),
            ...(avatar !== undefined && { avatar }),
        });
    }

    it("takes the avatar a heartbeat carries, and keeps it when the next carries none", () => {
        const store = new AgentStore();
        beat(store, { kind: "flic" });
        expect(store.get("a1")?.avatar).toEqual({ kind: "flic" });
        beat(store);
        expect(store.get("a1")?.avatar).toEqual({ kind: "flic" });
        beat(store, {});
        expect(store.get("a1")?.avatar).toEqual({});
    });

    it("tells its views when an agent's avatar changes, and not when it stays", async () => {
        const store = new AgentStore();
        beat(store, { kind: "flic" });
        await Promise.resolve();
        const updated: string[][] = [];
        store.subscribe(changes => updated.push([...changes.updated]));

        beat(store, { kind: "flic" });
        await Promise.resolve();
        expect(updated.flat()).toEqual([]);

        beat(store, { kind: "rule" });
        await Promise.resolve();
        expect(updated.flat()).toEqual(["a1"]);
    });
});
