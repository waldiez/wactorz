/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat composer's recipient list: who it offers, and in what order.
 */
import { describe, it, expect } from "vitest";
import { renderTargetSelect } from "../ui/chat/targetSelect";
import type { AgentInfo } from "../types/agent";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: `${name}-id`, name, state: "running", protected: false, ...over };
}

/** The agents the select offers, by the names it shows them under. */
function offered(agents: AgentInfo[], target = "main-id"): string[] {
    const select = document.createElement("select");
    renderTargetSelect(select, agents, target);
    return [...select.options].map(o => o.text.replace(/^@/, ""));
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

        renderTargetSelect(select, [agent("main"), agent("worker")], "worker-id");
        expect(select.value).toBe("worker-id");

        renderTargetSelect(select, [agent("main")], "worker-id");
        expect(select.value).toBe("main-id");
    });

    it("offers each agent by its id, under the name the page gives it", () => {
        // Two agents may share a name: the value says which, the label tells them apart.
        const select = document.createElement("select");
        const twins = [agent("counter", { id: "on-main" }), agent("counter", { id: "on-pi" })];

        renderTargetSelect(select, twins, "on-pi", a => `counter@${a.id}`);

        expect([...select.options].map(o => [o.value, o.text])).toEqual([
            ["on-main", "@counter@on-main"],
            ["on-pi", "@counter@on-pi"],
        ]);
        expect(select.value).toBe("on-pi");
    });
});
