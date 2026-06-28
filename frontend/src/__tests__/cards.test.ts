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

    it("stopped → Delete only (no Stop)", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("worker", { state: "stopped" }));
        expect(actions(c)).toEqual(["delete"]);
    });

    it("protected (but messageable) → Pause only, no Stop/Delete", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("main-actor", { protected: true }));
        expect(actions(c)).toEqual(["pause"]);
    });

    it("non-messageable system agent → no buttons", () => {
        const c = document.createElement("div");
        appendActionBtns(c, agent("io-agent"));
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

    it("routes action-button clicks through onCommand", () => {
        const cbs = cb();
        const card = buildWactorCard(agent("worker", { state: "running" }), 0, cbs);
        card.querySelector<HTMLButtonElement>('[data-action="pause"]')!.click();
        expect(cbs.onCommand).toHaveBeenCalledWith("worker", "pause", expect.anything());
    });

    it("shows the protected shield and hides destructive actions for protected agents", () => {
        const card = buildWactorCard(agent("main-actor", { protected: true }), 1000, cb());
        expect(card.querySelector(".af-card-protected")).not.toBeNull();
        expect(actions(card)).not.toContain("delete");
    });
});
