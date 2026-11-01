/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Who the chat talks to: an agent's id, with its name beside it. Ids differ from
 * names here, so nothing passes because the two happen to be spelled alike.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { ChatTarget } from "../ui/chat/chatTarget";
import { listen } from "../events";
import type { AgentInfo } from "../types/agent";
import type { View } from "../ui/shell/types";

function agent(name: string, id: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id, name, state: "running", protected: false, ...over };
}

function target(agents: AgentInfo[], view: View = "chat"): { t: ChatTarget; map: Map<string, AgentInfo> } {
    const map = new Map(agents.map(a => [a.id, a]));
    const t = new ChatTarget({ agents: () => map, getView: () => view, changed: vi.fn() });
    return { t, map };
}

beforeEach(() => localStorage.clear());

describe("a name two agents share", () => {
    const TWO = () => [
        agent("main", "main-id"),
        agent("counter", "counter-on-main"),
        agent("counter", "counter-on-pi", { node: "pi" }),
    ];

    it("names neither on its own, and offers both", () => {
        const { t } = target(TWO());

        expect(t.named("counter")).toBeUndefined();
        expect(t.choices("counter").map(a => a.id)).toEqual(["counter-on-main", "counter-on-pi"]);
    });

    it("by an address names that one, and leaves no choice", () => {
        const { t } = target(TWO());

        expect(t.named("counter@pi")?.id).toBe("counter-on-pi");
        expect(t.named("counter@main")?.id).toBe("counter-on-main");
        expect(t.choices("counter@pi")).toEqual([]);
    });

    it("chosen, is put in the conversation's address as name@node", () => {
        const { t } = target(TWO());
        const paths: string[][] = [];
        const listener = listen("af-view-changed", route => paths.push(route.path));

        t.move("counter-on-pi", "claim");
        listener();

        expect(t.address).toBe("counter@pi");
        expect(paths).toEqual([["counter@pi"]]);
    });

    it("asked for by name alone stays asked for, and its address is what was asked", () => {
        const { t } = target(TWO());

        t.want("counter");

        expect([t.id, t.waiting, t.address]).toEqual(["", true, "counter"]);
        expect(t.arrived()).toBeUndefined();
        expect(t.offered()).toEqual(["counter@main", "counter@pi"]);
    });

    it("offers nothing once one is chosen, or for a name no agent has", () => {
        const { t } = target(TWO());

        t.want("ghost");
        expect(t.offered()).toEqual([]);

        t.move("counter-on-pi", "claim");
        expect(t.offered()).toEqual([]);
    });

    it("says when the one chosen is called by another address, once", () => {
        const { t, map } = target([agent("main", "main-id"), agent("counter", "counter-on-main")]);
        t.move("counter-on-main", "claim");
        expect(t.relabelled()).toBeNull();

        map.set("counter-on-pi", agent("counter", "counter-on-pi", { node: "pi" }));
        expect(t.relabelled()).toBe("counter@main");
        expect(t.relabelled()).toBeNull();

        map.delete("counter-on-pi");
        expect(t.relabelled()).toBe("counter");
    });

    it("says nothing for a name asked for, nor for a move, which chooses anew", () => {
        const { t, map } = target(TWO());
        t.want("counter");
        expect(t.relabelled()).toBeNull();

        t.move("counter-on-pi", "claim");
        map.delete("counter-on-main");
        t.move("counter-on-pi", "claim");
        expect(t.relabelled()).toBeNull();
    });

    it("leaves main to the agent called main on main itself", () => {
        const { t } = target([...TWO(), agent("main", "main-on-pi", { node: "pi" })]);

        expect(t.mainId()).toBe("main-id");
    });
});

describe("a choice", () => {
    it("is kept by id, named by the agent's name, and remembered by id", () => {
        const { t } = target([agent("counter", "on-pi")]);
        const paths: string[][] = [];
        const listener = listen("af-view-changed", route => paths.push(route.path));

        t.move("on-pi", "claim");
        listener();

        expect([t.id, t.name, t.settled]).toEqual(["on-pi", "counter", true]);
        expect(localStorage.getItem("wactorz-chat-agent-id")).toBe("on-pi");
        // The address says the name: readable, and it outlives a spawn under a new id.
        expect(paths).toEqual([["counter"]]);
    });

    it("filled for the person is neither settled nor remembered", () => {
        const { t } = target([agent("main", "main-id")]);

        t.move("main-id", "default");

        expect(t.settled).toBe(false);
        expect(localStorage.getItem("wactorz-chat-agent-id")).toBeNull();
    });

    it("keeps its name once its agent has gone from the list", () => {
        const { t, map } = target([agent("counter", "on-pi")]);
        t.move("on-pi", "claim");

        map.clear();

        expect(t.name).toBe("counter");
    });
});

describe("a name an address asks for", () => {
    it("stands as the choice until its agent arrives, which it then names", () => {
        const { t, map } = target([]);

        t.want("counter");

        expect([t.id, t.name, t.waiting, t.settled]).toEqual(["", "counter", true, true]);
        expect(t.settleOn()).toEqual({ changed: false });

        map.set("on-pi", agent("counter", "on-pi"));

        expect(t.settleOn().arrived?.id).toBe("on-pi");
    });
});

describe("settling on a conversation", () => {
    it("fills an empty choice with main, by its id", () => {
        const { t } = target([agent("main", "main-id"), agent("counter", "on-pi")]);

        expect(t.settleOn()).toEqual({ changed: true });
        expect(t.id).toBe("main-id");
    });

    it("opens the remembered agent, by its id", () => {
        localStorage.setItem("wactorz-chat-agent-id", "on-pi");
        const { t } = target([agent("main", "main-id"), agent("counter", "on-pi")]);

        t.settleOn();

        expect(t.id).toBe("on-pi");
    });
});

describe("a target that has gone", () => {
    it("moves to main, and says what was lost", () => {
        const { t, map } = target([agent("main", "main-id"), agent("counter", "on-pi")]);
        t.move("on-pi", "claim");
        map.delete("on-pi");

        expect(t.replaceIfGone()).toBe("counter");
        expect(t.id).toBe("main-id");
    });

    it("stays while it is still there", () => {
        const { t } = target([agent("main", "main-id"), agent("counter", "on-pi")]);
        t.move("on-pi", "claim");

        expect(t.replaceIfGone()).toBeNull();
        expect(t.id).toBe("on-pi");
    });
});
