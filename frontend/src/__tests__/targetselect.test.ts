/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat composer's recipient list: who it offers, and in what order.
 */
import { describe, it, expect } from "vitest";
import { renderTargetSelect } from "../ui/dashboard/targetSelect";
import type { AgentInfo } from "../types/agent";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: `${name}-id`, name, state: "running", protected: false, ...over };
}

function offered(agents: AgentInfo[], target = "main"): string[] {
    const select = document.createElement("select");
    renderTargetSelect(select, agents, target);
    return [...select.options].map(o => o.value);
}

describe("renderTargetSelect", () => {
    it("offers the pinned agents first, then the rest by name", () => {
        expect(offered([agent("zeta"), agent("catalog"), agent("alpha"), agent("main")])).toEqual([
            "main",
            "catalog",
            "alpha",
            "zeta",
        ]);
    });

    it("leaves out the chat transport, announced as an agent on every broker reconnect", () => {
        // Offered for the seconds until the next sync, it was chosen, and the
        // thread it opened belonged to nobody.
        expect(offered([agent("main"), agent("io-gateway"), agent("worker")])).toEqual(["main", "worker"]);
    });

    it("leaves out agents that cannot be messaged", () => {
        expect(
            offered([agent("main"), agent("monitor-agent"), agent("guarded", { protected: true })]),
        ).toEqual(["main"]);
    });

    it("shows the chosen target when it is on the list, and the first one otherwise", () => {
        const select = document.createElement("select");

        renderTargetSelect(select, [agent("main"), agent("worker")], "worker");
        expect(select.value).toBe("worker");

        renderTargetSelect(select, [agent("main")], "worker");
        expect(select.value).toBe("main");
    });
});
