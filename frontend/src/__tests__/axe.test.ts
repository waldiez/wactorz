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

import type { CardDashboard } from "../ui/CardDashboard";
import type { AgentInfo } from "../types/agent";
import type { FeedItem } from "../types/feed";
import { emit } from "../events";
import { blockingFindings, isAccepted } from "./axe";
import { byRole } from "./queries";
import { goTo, openDashboard } from "./dashboard";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

const AGENTS = [
    agent("main", { essential: true, protected: true }),
    agent("weather", { task: "the forecast", messagesProcessed: 3, costUsd: 0.01 }),
    agent("flic", { state: "stopped" }),
    // A name two agents share: each card names its node.
    agent("weather", { id: "weather-pi", node: "pi" }),
];

describe("the dashboard passes axe", () => {
    let cd: CardDashboard;

    beforeEach(() => {
        document.body.replaceChildren();
        localStorage.clear();
        ({ dashboard: cd } = openDashboard(AGENTS));
        // A reply long enough to open in place, from a turn: a row holding
        // buttons of its own, which a row that is itself a button cannot.
        const reply = { type: "chat", label: "Here is the plan:\n1. pair it\n2. wire it", agentName: "main" };
        emit("af-feed-push", { item: { ...reply, timestamp: 1, turn: "t1" } as FeedItem });
    });

    afterEach(() => {
        cd.destroy();
    });

    it.each(["overview", "feed", "chat", "settings"])("on the %s view", async view => {
        goTo(view);

        // Something to judge: an empty page passes every rule.
        expect(document.querySelector(`[data-testid="view-${view}"]`)).not.toBeNull();
        document.querySelector<HTMLButtonElement>(".af-feed-toggle")?.click();
        expect(await blockingFindings(document.body)).toEqual([]);
    });

    it("on the models view, a model open", async () => {
        const version = (n: number) => ({
            name: "hvac",
            version: n,
            sha256: `s${n}`,
            format: "onnx",
            size: 2048,
            runs_code: false,
            added_at: 1_700_000_000 + n,
            metadata: { source: "cli" },
            reference: `hvac:${n}`,
        });
        const pointer = (which: string, n: number, previous: number | null) => ({
            name: "hvac",
            pointer: which,
            version: n,
            previous,
            moved_at: 1,
            by: "cli",
            reason: null,
            holds: true,
        });
        const pointers = { active: pointer("active", 2, 1), shadow: pointer("shadow", 3, null) };
        vi.stubGlobal(
            "fetch",
            vi.fn(async (url: string) => {
                if (url.endsWith("/moves")) {
                    return Response.json({
                        moves: [
                            {
                                at: 1,
                                pointer: "shadow",
                                change: "moved",
                                version: 3,
                                from: null,
                                by: "chat",
                                reason: "why",
                            },
                        ],
                    });
                }
                if (url.endsWith("/api/models")) {
                    return Response.json({
                        models: [{ name: "hvac", newest: version(3), versions: 3, pointers }],
                    });
                }
                return Response.json({ name: "hvac", versions: [1, 2, 3].map(version), pointers });
            }),
        );

        goTo("models", "hvac");
        await vi.waitFor(() => expect(document.querySelector(".af-model-detail")).not.toBeNull());

        expect(await blockingFindings(document.body)).toEqual([]);
        vi.unstubAllGlobals();
    });

    it("on the Inbox, with a question waiting", async () => {
        goTo("inbox");
        emit("af-question-book", {
            agentId: "a1",
            questions: [
                {
                    id: "q",
                    agent_id: "a1",
                    agent: "asker",
                    text: "Is the probe in place?",
                    options: ["Yes", "No"],
                    context: "The reading jumped.",
                },
            ],
        });

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
            goTo("chat");
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
