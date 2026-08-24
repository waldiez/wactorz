/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import {
    buildHostBar,
    buildStatCards,
    appendActionBtns,
    buildWactorCard,
    type WactorCardCallbacks,
} from "../ui/dashboard/cards";
import type { AgentInfo } from "../types/agent";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...over };
}

const cb = (): WactorCardCallbacks => ({ onChat: vi.fn(), onCommand: vi.fn() });
const actions = (el: HTMLElement) =>
    [...el.querySelectorAll<HTMLElement>("[data-action]")].map(b => b.dataset["action"]);

describe("buildHostBar", () => {
    it("renders dashes and 0% widths when stats are unknown", () => {
        const bar = buildHostBar(null, null, null);
        expect(bar.querySelector(".af-host-cpu-val")!.textContent).toBe("—");
        expect(bar.querySelector(".af-host-mem-val")!.textContent).toBe("—");
        expect(bar.querySelector<HTMLElement>(".af-host-bar-fill-cpu")!.style.width).toBe("0.0%");
    });

    it("renders GB memory text and clamped widths with a total", () => {
        const bar = buildHostBar(150, 4096, 8192); // cpu clamps to 100
        expect(bar.querySelector(".af-host-cpu-val")!.textContent).toBe("150.0%");
        expect(bar.querySelector<HTMLElement>(".af-host-bar-fill-cpu")!.style.width).toBe("100.0%");
        expect(bar.querySelector(".af-host-mem-val")!.textContent).toBe("4.0 / 8.0 GB");
    });

    it("renders raw MB when no memory total is known", () => {
        const bar = buildHostBar(10, 512, null);
        expect(bar.querySelector(".af-host-mem-val")!.textContent).toBe("512 MB");
    });
});

describe("buildStatCards", () => {
    const base = { totalMessages: 5, totalCostUsd: 1.5, feedCount: 3, costLimit: null };

    it("renders the four summary cards", () => {
        const c = document.createElement("div");
        buildStatCards(c, { ...base, agents: [agent("a"), agent("b", { state: "stopped" })] });
        const labels = [...c.querySelectorAll(".af-stat-label")].map(e => e.textContent);
        expect(labels).toEqual(["Wactorz", "Messages", "Cost", "Feed Events"]);
        expect(c.querySelectorAll(".af-stat-card")[0]!.querySelector(".af-stat-detail")!.textContent).toBe(
            "1 running", // only one of two agents is running
        );
    });

    it("colours the cost card by how close the spend is to the limit", () => {
        const detailOf = (limit: Record<string, unknown>): { detail: string; accent: string } => {
            const c = document.createElement("div");
            buildStatCards(c, { ...base, agents: [], costLimit: limit as never });
            const card = c.querySelectorAll(".af-stat-card")[2]!;
            return {
                detail: card.querySelector(".af-stat-detail")!.textContent!,
                accent: (card as HTMLElement).style.borderColor,
            };
        };

        const under = detailOf({ limit_usd: 10, spend_usd: 1, pct_used: 10, period: "monthly" });
        const warn = detailOf({
            limit_usd: 10,
            spend_usd: 8,
            pct_used: 80,
            period: "monthly",
            warning: true,
        });
        const over = detailOf({
            limit_usd: 10,
            spend_usd: 10,
            pct_used: 100,
            period: "monthly",
            limit_reached: true,
        });

        // Reaching the limit is the only state that turns the card red; a
        // warning is amber, and so is the under-limit card, so the bar inside
        // is what distinguishes those two.
        expect(over.accent).not.toBe(warn.accent);
        expect(under.detail).toContain("$1.0000 / $10.00 this month");
    });

    it("names the period the limit applies to", () => {
        const periodOf = (period: string): string => {
            const c = document.createElement("div");
            buildStatCards(c, {
                ...base,
                agents: [],
                costLimit: { limit_usd: 5, spend_usd: 1, pct_used: 20, period } as never,
            });
            return c.querySelectorAll(".af-stat-card")[2]!.querySelector(".af-stat-detail")!.textContent!;
        };

        // The number means nothing without the window it accrued over.
        expect(periodOf("daily")).toContain("today");
        expect(periodOf("weekly")).toContain("this week");
        expect(periodOf("monthly")).toContain("this month");
    });

    it("falls back to summing per-agent metrics when totals are null", () => {
        const c = document.createElement("div");
        buildStatCards(c, {
            ...base,
            totalMessages: null,
            totalCostUsd: null,
            agents: [agent("a", { messagesProcessed: 4, costUsd: 0.5 })],
        });
        const cards = c.querySelectorAll(".af-stat-card");
        expect(cards[1]!.querySelector(".af-stat-value")!.textContent).toBe("4"); // messages
        expect(cards[2]!.querySelector(".af-stat-value")!.textContent).toBe("$0.5000"); // cost
    });

    it("renders the spend-limit progress bar when a limit is set", () => {
        const c = document.createElement("div");
        buildStatCards(c, {
            ...base,
            agents: [agent("a")],
            costLimit: { limit_usd: 10, spend_usd: 2.5, pct_used: 25, period: "monthly", warning: false },
        });
        const costCard = c.querySelectorAll(".af-stat-card")[2]!;
        expect(costCard.querySelector(".af-stat-detail")!.textContent).toContain(
            "$2.5000 / $10.00 this month",
        );
    });
});

describe("appendActionBtns", () => {
    it("running → Pause + Stop + Delete", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("worker", { state: "running" }));
        expect(actions(c)).toEqual(["pause", "stop", "delete"]);
    });

    it("paused → Resume + Stop + Delete", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("worker", { state: "paused" }));
        expect(actions(c)).toEqual(["resume", "stop", "delete"]);
    });

    it("stopped → Start and Delete (no Stop)", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("worker", { state: "stopped" }));
        // Start has to be offered here, or Stop is a one-way door.
        expect(actions(c)).toEqual(["start", "delete"]);
    });

    it("running → no Start", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("worker", { state: "running" }));
        expect(actions(c)).not.toContain("start");
    });

    it("protected (but messageable) → Pause only, no Stop/Delete", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("main", { protected: true }));
        expect(actions(c)).toEqual(["pause"]);
    });

    it("non-messageable system agent → no buttons", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("monitor-agent"));
        expect(actions(c)).toEqual([]);
    });
});

describe("buildWactorCard", () => {
    it("renders header, task and a working Chat button", () => {
        const cbs = cb();
        const a = agent("worker", { task: "doing things", messagesProcessed: 2 });
        const card = buildWactorCard(a, 0, cbs);
        expect(card.dataset["id"]).toBe("worker");
        expect(card.querySelector(".af-card-name")!.textContent).toBe("worker");
        expect(card.querySelector(".af-card-task")!.textContent).toBe("doing things");
        card.querySelector<HTMLButtonElement>(".af-chat-btn")!.click();
        expect(cbs.onChat).toHaveBeenCalledWith(a);
    });

    it("gives a system agent no Chat button", () => {
        // monitor-agent is plumbing, not a correspondent — offering Chat would send
        // messages nothing answers.
        const card = buildWactorCard(agent("monitor-agent"), 0, cb());

        expect(card.querySelector(".af-chat-btn")).toBeNull();
    });

    it("ignores clicks on a disabled action button", () => {
        const cbs = cb();
        const card = buildWactorCard(agent("worker"), 0, cbs);
        const btn = card.querySelector<HTMLButtonElement>("[data-action]")!;
        btn.disabled = true;

        btn.click();

        // A disabled button is how an in-flight command is shown; firing it
        // again would send the same command twice.
        expect(cbs.onCommand).not.toHaveBeenCalled();
    });

    it("shows cost only once there is some", () => {
        const withCost = buildWactorCard(agent("a", { costUsd: 0.25 }), 0, cb());
        const without = buildWactorCard(agent("b", { costUsd: 0 }), 0, cb());

        expect(withCost.querySelector(".af-card-meta")!.textContent).toContain("$0.2500");
        expect(without.querySelector(".af-card-meta")!.textContent).not.toContain("$");
    });

    it("abbreviates large token counts", () => {
        const card = buildWactorCard(agent("a", { inputTokens: 2_400_000, outputTokens: 1_500 }), 0, cb());

        const line = card.textContent!;
        expect(line).toContain("2.4M");
        expect(line).toContain("1.5k");
    });

    it("routes action-button clicks through onCommand", () => {
        const cbs = cb();
        const card = buildWactorCard(agent("worker", { state: "running" }), 0, cbs);
        card.querySelector<HTMLButtonElement>('[data-action="pause"]')!.click();
        expect(cbs.onCommand).toHaveBeenCalledWith("worker", "pause", expect.anything());
    });

    it("shows the protected shield and hides destructive actions for protected agents", () => {
        const card = buildWactorCard(agent("main", { protected: true }), 1000, cb());
        expect(card.querySelector(".af-card-protected")).not.toBeNull();
        expect(actions(card)).not.toContain("delete");
    });

    it("shows compact in/out tokens for an LLM agent", () => {
        const card = buildWactorCard(agent("llm", { inputTokens: 12480, outputTokens: 900 }), 0, cb());
        const tok = card.querySelector(".af-card-tokens");
        expect(tok).not.toBeNull();
        expect(tok!.textContent).toContain("12.5k↑");
        expect(tok!.textContent).toContain("900↓");
    });

    it("omits the token widget for a non-LLM agent (no token fields)", () => {
        const card = buildWactorCard(agent("worker"), 0, cb());
        expect(card.querySelector(".af-card-tokens")).toBeNull();
    });

    it("omits tokens and cost for an idle LLM agent reporting zeros", () => {
        // e.g. home-assistant-agent that hasn't made a call: 0/0 tokens, $0 cost.
        const card = buildWactorCard(agent("ha", { inputTokens: 0, outputTokens: 0, costUsd: 0 }), 0, cb());
        expect(card.querySelector(".af-card-tokens")).toBeNull();
        expect(card.querySelector(".af-card-meta")!.textContent).not.toContain("$");
    });
});
