/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Every view of the dashboard, built as the page builds it, passes axe.
 *
 * One test per view rather than per component: a component can be fine alone
 * and wrong where it is put, such as a list item outside its list.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { CardDashboard } from "../ui/CardDashboard";
import type { AgentInfo } from "../types/agent";
import type { FeedItem } from "../types/feed";
import { emit } from "../events";
import { blockingFindings, isAccepted } from "./axe";
import { byRole } from "./queries";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

const AGENTS = [
    agent("main", { essential: true, protected: true }),
    agent("weather", { task: "the forecast", messagesProcessed: 3, costUsd: 0.01 }),
    agent("flic", { state: "stopped" }),
];

describe("the dashboard passes axe", () => {
    let cd: CardDashboard;

    beforeEach(() => {
        document.body.replaceChildren();
        localStorage.clear();
        cd = new CardDashboard();
        cd.show(AGENTS);
        // A reply long enough to open in place, from a turn: a row holding
        // buttons of its own, which a row that is itself a button cannot.
        const reply = { type: "chat", label: "Here is the plan:\n1. pair it\n2. wire it", agentName: "main" };
        emit("af-feed-push", { item: { ...reply, timestamp: 1, turn: "t1" } as FeedItem });
    });

    afterEach(() => {
        cd.destroy();
    });

    it.each(["overview", "feed", "chat", "settings"])("on the %s view", async view => {
        (cd as unknown as { _setView(v: string): void })._setView(view);

        // Something to judge: an empty page passes every rule.
        expect(document.querySelector(`[data-testid="view-${view}"]`)).not.toBeNull();
        document.querySelector<HTMLButtonElement>(".af-feed-toggle")?.click();
        expect(await blockingFindings(document.body)).toEqual([]);
    });

    it("on the chat view, with the command list open", async () => {
        const commands = [
            { usage: "/plans", typed: "/plans", summary: "list pending pipeline proposals" },
            { usage: "/plans approve <id>", typed: "/plans approve", summary: "execute a proposed pipeline" },
        ];
        vi.stubGlobal(
            "fetch",
            vi.fn(
                async () =>
                    new Response(JSON.stringify({ commands }), {
                        headers: { "Content-Type": "application/json" },
                    }),
            ),
        );
        try {
            (cd as unknown as { _setView(v: string): void })._setView("chat");
            const input = document.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
            input.value = "/pl";
            input.dispatchEvent(new Event("input"));
            await vi.waitFor(() => expect(byRole(document.body, "listbox", "Commands").hidden).toBe(false));

            expect(input.getAttribute("aria-activedescendant")).toBeTruthy();
            expect(await blockingFindings(document.body)).toEqual([]);
        } finally {
            vi.unstubAllGlobals();
        }
    });
});

describe("an accepted finding", () => {
    const list = [{ rule: "aria-allowed-role", selector: ".af-card", reason: "a test" }];

    it("covers its rule on a matching element", () => {
        expect(isAccepted("aria-allowed-role", "#overview > .af-card", list)).toBe(true);
    });

    it("covers neither another rule nor another element", () => {
        expect(isAccepted("button-name", ".af-card", list)).toBe(false);
        expect(isAccepted("aria-allowed-role", ".af-node-card", list)).toBe(false);
    });
});
