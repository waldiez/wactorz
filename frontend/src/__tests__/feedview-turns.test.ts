/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { buildFeedView, DEFAULT_FILTERS, type FeedFilters } from "../ui/dashboard/feedView";
import { toEntry } from "../ui/dashboard/appLogs";
import { logFeedItem } from "../agents/mapping";
import { ServerEventRouter } from "../io/ServerEventRouter";
import type { AppLogItem, FeedItem } from "../types/feed";
import type { LogPayload } from "../types/agent";

const NOW = 1_700_000_000_000;

function agentRow(over: Partial<FeedItem> = {}): FeedItem {
    return { type: "chat", label: "working on it", agentName: "weather", timestamp: NOW, ...over };
}

function appRow(over: Partial<AppLogItem> = {}): AppLogItem {
    return {
        source: "app",
        ts: NOW / 1000,
        level: "INFO",
        origin: "wactorz.agents.main",
        text: "routing",
        ...over,
    };
}

function view(items: (FeedItem | AppLogItem)[], filters: Partial<FeedFilters> = {}) {
    const changes: FeedFilters[] = [];
    const root = buildFeedView(items, {
        filters: { ...DEFAULT_FILTERS, source: "all", ...filters },
        onFiltersChange: f => changes.push(f),
    });
    document.body.appendChild(root);
    const shown = (): string[] =>
        [...root.querySelectorAll<HTMLElement>(".af-feed-item")]
            .filter(r => r.style.display !== "none" && !r.hidden)
            .map(r => r.querySelector(".af-feed-text")?.textContent ?? "");
    return { root, changes, shown };
}

beforeEach(() => {
    document.body.innerHTML = "";
});

describe("rows say which turn and agent they belong to", () => {
    it("an agent's event, and an application log line", () => {
        const { root } = view([
            agentRow({ turn: "t1abcdef9999" }),
            appRow({ turn: "t1abcdef9999", agent: "main", ts: NOW / 1000 + 1 }),
        ]);
        const [event, line] = [...root.querySelectorAll<HTMLElement>(".af-feed-item")];

        expect(event!.dataset["turn"]).toBe("t1abcdef9999");
        expect(event!.dataset["agent"]).toBe("weather");
        expect(line!.dataset["agent"]).toBe("main");
        expect(line!.querySelector(".af-feed-agent-chip")?.textContent).toBe("main");
        // Short on the row, whole in the title.
        expect(line!.querySelector(".af-feed-turn")?.textContent).toBe("t1abcd");
        expect(line!.querySelector<HTMLElement>(".af-feed-turn")?.title).toContain("t1abcdef9999");
    });

    it("a person's own message names no agent to narrow to", () => {
        const { root } = view([agentRow({ role: "user", agentName: "user" })]);
        const row = root.querySelector<HTMLElement>(".af-feed-item")!;

        expect(row.dataset["agent"]).toBeUndefined();
        expect(row.querySelector(".af-feed-pick")).toBeNull();
    });
});

describe("narrowing the feed", () => {
    const rows = [
        agentRow({ label: "a", turn: "t1", timestamp: NOW }),
        appRow({ text: "b", turn: "t1", agent: "main", ts: NOW / 1000 + 1 }),
        appRow({ text: "c", turn: "t2", agent: "weather", ts: NOW / 1000 + 2 }),
        agentRow({ label: "d", agentName: "planner", timestamp: NOW + 3000 }),
    ];

    it("to one turn from its chip, showing everything that turn did", () => {
        const { root, changes, shown } = view(rows, { source: "agent" });
        root.querySelector<HTMLButtonElement>('[data-set-turn="t1"]')!.click();

        expect(shown()).toEqual(["a", "b"]);
        expect(changes.at(-1)).toMatchObject({ turn: "t1", source: "all" });
        expect(root.querySelector<HTMLSelectElement>("select")!.value).toBe("all");
    });

    it("without opening the row it was picked from, by mouse or by keyboard", () => {
        // A multi-line message: the row opens in place when clicked.
        const { root } = view([agentRow({ label: "line one\nline two", turn: "t1" })]);
        const row = root.querySelector<HTMLElement>(".af-feed-item")!;
        const chip = row.querySelector<HTMLButtonElement>(".af-feed-turn")!;
        expect(row.getAttribute("aria-expanded")).toBe("false");

        chip.click();
        chip.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        chip.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));

        expect(row.getAttribute("aria-expanded")).toBe("false");
        // The row itself still opens.
        row.click();
        expect(row.getAttribute("aria-expanded")).toBe("true");
    });

    it("to one agent from its name", () => {
        const { root, shown } = view(rows);

        root.querySelector<HTMLButtonElement>('.af-feed-agent[data-set-agent="planner"]')!.click();

        expect(shown()).toEqual(["d"]);
    });

    it("and back, from the chip in the toolbar", () => {
        const { root, changes, shown } = view(rows);
        root.querySelector<HTMLButtonElement>('[data-set-turn="t2"]')!.click();
        const active = root.querySelector<HTMLButtonElement>(".af-feed-active-chip")!;
        expect(active.textContent).toBe("turn t2 ×");

        active.click();

        expect(shown()).toEqual(["a", "b", "c", "d"]);
        expect(changes.at(-1)).toMatchObject({ turn: "" });
        expect(root.querySelector(".af-feed-active-chip")).toBeNull();
    });

    it("and back from an agent, leaving the turn as it was", () => {
        const { root, changes, shown } = view(rows, { turn: "t1", agent: "main" });
        const chips = [...root.querySelectorAll<HTMLButtonElement>(".af-feed-active-chip")];
        expect(chips.map(c => c.textContent)).toEqual(["turn t1 ×", "agent main ×"]);

        chips[1]!.click();

        expect(shown()).toEqual(["a", "b"]);
        expect(changes.at(-1)).toMatchObject({ turn: "t1", agent: "" });
    });

    it("other keys on a chip still reach the row", () => {
        const { root } = view([agentRow({ turn: "t1" })]);
        const row = root.querySelector<HTMLElement>(".af-feed-item")!;
        const seen: string[] = [];
        row.addEventListener("keydown", e => seen.push(e.key));

        const chip = row.querySelector<HTMLButtonElement>(".af-feed-turn")!;
        chip.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
        chip.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

        expect(seen).toEqual(["Tab"]);
    });

    it("by turn and agent together", () => {
        const { shown } = view(rows, { turn: "t1", agent: "main" });

        expect(shown()).toEqual(["b"]);
    });

    it("a row that belongs to no turn is left out by a turn filter", () => {
        const { shown } = view(rows, { turn: "t1" });

        expect(shown()).not.toContain("d");
    });
});

describe("where the turn comes from", () => {
    it("an application log entry from the server", () => {
        expect(toEntry({ ts: 1, text: "x", turn: "t1", agent: "main" })).toMatchObject({
            turn: "t1",
            agent: "main",
        });
        expect(toEntry({ ts: 1, text: "x", turn: "", agent: 3 })).not.toHaveProperty("turn");
        expect(toEntry({ ts: 1, text: "x", agent: 3 })).not.toHaveProperty("agent");
    });

    it("an agent's log event, through the router into a feed row", () => {
        const router = new ServerEventRouter();
        const seen: LogPayload[] = [];
        router.on("logs", p => seen.push(p));

        router.route("agents/abc/logs", { message: "busy", name: "weather", turn: "t9" });
        router.route("agents/abc/logs", { message: "idle", name: "weather" });

        expect(seen.map(p => p.turn)).toEqual(["t9", undefined]);
        expect(logFeedItem(seen[0]!, NOW)).toMatchObject({ turn: "t9", label: "busy" });
        expect(logFeedItem(seen[1]!, NOW)).not.toHaveProperty("turn");
    });
});

vi.restoreAllMocks();
