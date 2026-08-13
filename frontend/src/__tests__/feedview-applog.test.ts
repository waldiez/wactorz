/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Application-log rows in the activity view.
 *
 * ⚠ These rows render the one thing the feed never had to before: whole
 * untrusted records, including tracebacks an agent can be induced to emit. The
 * escaping tests below are the point of this file, not a formality — a
 * `<pre>` holding the full record is exactly where someone reaches for
 * `innerHTML`, and that would be stored XSS in the feature whose purpose is
 * displaying attacker-influenceable strings.
 */
import { describe, it, expect, vi } from "vitest";
import {
    appLogItemEl,
    applyFilters,
    buildFeedView,
    shortOrigin,
    MAX_ROWS,
    appendFeedItemToView,
    DEFAULT_FILTERS,
    type FeedFilters,
} from "../ui/dashboard/feedView";
import type { AppLogItem, FeedItem } from "../types/feed";

const log = (over: Partial<AppLogItem> = {}): AppLogItem => ({
    source: "app",
    ts: 1_700_000_000,
    level: "INFO",
    origin: "wactorz.web.app",
    text: "listening on 127.0.0.1:8888",
    ...over,
});

const agentItem = (over: Partial<FeedItem> = {}): FeedItem => ({
    type: "chat",
    label: "hello",
    agentName: "worker",
    timestamp: 1_700_000_000_000,
    ...over,
});

const filters = (over: Partial<FeedFilters> = {}): FeedFilters => ({
    ...DEFAULT_FILTERS,
    source: "all",
    level: "DEBUG",
    hideHeartbeats: false,
    ...over,
});

function render(item: AppLogItem): HTMLElement {
    const c = document.createElement("div");
    appLogItemEl(c, item);
    return c.querySelector<HTMLElement>(".af-feed-item")!;
}

describe("an application-log row", () => {
    it("is marked by source and level so filters can find it", () => {
        const row = render(log({ level: "ERROR" }));
        expect(row.dataset["source"]).toBe("app");
        expect(row.dataset["level"]).toBe("ERROR");
    });

    it("colours its border from a lookup, never from the payload", () => {
        // ⚠ `af-feed-lvl-${level}` would let a crafted record name any class in
        // the stylesheet; `level` arrives over the wire.
        expect(render(log({ level: "ERROR" })).className).toContain("af-feed-lvl-error");
        expect(render(log({ level: "</style><script>" as never })).className).not.toContain("script");
    });

    it("shortens a long logger name and keeps the whole one in the tooltip", () => {
        const row = render(log({ origin: "wactorz.agents.installer" }));
        const origin = row.querySelector<HTMLElement>(".af-feed-agent")!;
        expect(origin.textContent).toBe("agents.installer");
        expect(origin.title).toBe("wactorz.agents.installer");
    });

    it("leaves a short logger name alone", () => {
        expect(shortOrigin("aiohttp")).toBe("aiohttp");
        expect(shortOrigin("wactorz.web")).toBe("wactorz.web");
    });
});

describe("a multi-line record", () => {
    const traceback = 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: boom';

    it("shows one line collapsed and the whole record when expanded", () => {
        const row = render(log({ text: traceback }));
        const full = row.querySelector<HTMLElement>(".af-feed-full")!;

        expect(row.querySelector(".af-feed-text")!.textContent).toBe("Traceback (most recent call last):");
        expect(full.hidden).toBe(true);

        row.click();

        expect(full.hidden).toBe(false);
        expect(full.textContent).toBe(traceback);
        expect(row.getAttribute("aria-expanded")).toBe("true");
    });

    it("expands from the keyboard too", () => {
        const row = render(log({ text: traceback }));
        row.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        expect(row.querySelector<HTMLElement>(".af-feed-full")!.hidden).toBe(false);
    });

    it("leaves a short single-line record unexpandable", () => {
        const row = render(log({ text: "started" }));
        expect(row.classList.contains("af-feed-expandable")).toBe(false);
        expect(row.querySelector(".af-feed-full")).toBeNull();
    });
});

describe("untrusted text renders as text", () => {
    const hostile = "<script>alert(1)</script>\n<img src=x onerror=alert(2)>";

    it("in the collapsed row", () => {
        const row = render(log({ text: hostile }));
        const text = row.querySelector<HTMLElement>(".af-feed-text")!;
        expect(text.querySelector("script")).toBeNull();
        expect(text.textContent).toContain("<script>");
    });

    it("in the expanded pre", () => {
        const row = render(log({ text: hostile }));
        row.click();
        const full = row.querySelector<HTMLElement>(".af-feed-full")!;
        expect(full.querySelector("script")).toBeNull();
        expect(full.querySelector("img")).toBeNull();
        expect(full.textContent).toBe(hostile);
    });

    it("in the logger name", () => {
        const row = render(log({ origin: "<script>alert(1)</script>" }));
        expect(row.querySelector(".af-feed-agent")!.querySelector("script")).toBeNull();
    });
});

describe("the toolbar filters", () => {
    const view = (over: Partial<FeedFilters> = {}) =>
        buildFeedView(
            [agentItem(), log({ level: "DEBUG", text: "chatty" }), log({ level: "ERROR", text: "boom" })],
            {
                filters: filters(over),
                onFiltersChange: vi.fn(),
            },
        );

    const visible = (wrap: HTMLElement) =>
        [...wrap.querySelectorAll<HTMLElement>(".af-feed-item")].filter(r => !r.hidden);

    it("show=agents hides every log row", () => {
        const rows = visible(view({ source: "agent" }));
        expect(rows).toHaveLength(1);
        expect(rows[0]!.dataset["source"]).toBe("agent");
    });

    it("show=logs hides agent activity", () => {
        expect(visible(view({ source: "app" })).every(r => r.dataset["source"] === "app")).toBe(true);
    });

    it("the level filter is 'this and above'", () => {
        const rows = visible(view({ source: "app", level: "ERROR" }));
        expect(rows).toHaveLength(1);
        expect(rows[0]!.dataset["level"]).toBe("ERROR");
    });

    it("never hides an agent row for its level", () => {
        // Agent events are typed, not levelled — a level filter must not silently
        // empty the half of the feed that has no levels at all.
        expect(
            visible(view({ source: "all", level: "CRITICAL" })).some(r => r.dataset["source"] === "agent"),
        ).toBe(true);
    });

    it("shows a row whose level nobody recognises", () => {
        // Defensive: the fetch boundary normally coerces an unrankable level to
        // INFO, so this is for rows that arrive by any other route. Hiding what
        // cannot be ranked is the one wrong answer — it is invisible.
        const feed = document.createElement("div");
        appLogItemEl(feed, log({ level: "TRACE" as never }));
        applyFilters(feed, filters({ source: "app", level: "ERROR" }));
        expect(feed.querySelector<HTMLElement>(".af-feed-item")!.hidden).toBe(false);
    });

    it("search matches case-insensitively and hides the rest", () => {
        const rows = visible(view({ search: "BOOM" }));
        expect(rows).toHaveLength(1);
        expect(rows[0]!.textContent).toContain("boom");
    });

    it("filters in place, keeping rows in the DOM for when the filter changes", () => {
        const wrap = view({ search: "nothing matches this" });
        expect(wrap.querySelectorAll(".af-feed-item").length).toBe(3);
        expect(visible(wrap)).toHaveLength(0);
        expect(wrap.querySelector<HTMLElement>(".af-feed-empty")!.hidden).toBe(false);
    });
});

describe("controls the current source has no use for", () => {
    const build = (source: FeedFilters["source"]) =>
        buildFeedView([], { filters: filters({ source }), onFiltersChange: vi.fn() });

    const level = (w: HTMLElement) => w.querySelectorAll<HTMLSelectElement>(".af-feed-select")[1]!;
    const heartbeats = (w: HTMLElement) => w.querySelector<HTMLButtonElement>(".af-mini-btn")!;

    it("disables the level filter while only agents are shown", () => {
        // Agent events are typed, not levelled — a live control governing
        // nothing reads as a broken filter.
        expect(level(build("agent")).disabled).toBe(true);
        expect(heartbeats(build("agent")).disabled).toBe(false);
    });

    it("disables the heartbeat toggle while only logs are shown", () => {
        expect(heartbeats(build("app")).disabled).toBe(true);
        expect(level(build("app")).disabled).toBe(false);
    });

    it("enables both when everything is shown", () => {
        const wrap = build("all");
        expect(level(wrap).disabled).toBe(false);
        expect(heartbeats(wrap).disabled).toBe(false);
    });

    it("re-syncs when the source changes", () => {
        const wrap = build("all");
        const source = wrap.querySelector<HTMLSelectElement>(".af-feed-select")!;
        source.value = "agent";
        source.dispatchEvent(new Event("change"));
        expect(level(wrap).disabled).toBe(true);
    });
});

describe("the list stays bounded", () => {
    it("drops the oldest rows past the cap", () => {
        // ⚠ Nothing used to remove a row. `feedItems` is capped, but that only
        // bounds a rebuild — appending never evicted, so a long session grew
        // the DOM without limit, and polling the log makes that faster.
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"></div>`;
        const f = filters();

        for (let i = 0; i < MAX_ROWS + 25; i++) {
            appendFeedItemToView(root, log({ ts: 1_700_000_000 + i, text: `line ${i}` }), f);
        }

        const rows = root.querySelectorAll(".af-feed-item");
        expect(rows).toHaveLength(MAX_ROWS);
        // The survivors are the newest — an activity view that drops the latest
        // row is worse than one that grows.
        expect(rows[rows.length - 1]!.textContent).toContain(`line ${MAX_ROWS + 24}`);
    });
});

describe("the application log's own controls", () => {
    it("are absent unless the caller offers them", () => {
        const wrap = buildFeedView([], { filters: filters(), onFiltersChange: vi.fn() });
        expect(wrap.querySelector(".af-feed-refresh")).toBeNull();
        expect(wrap.querySelector(".af-feed-follow")).toBeNull();
    });

    it("refresh asks the caller to fetch again", () => {
        const onRefresh = vi.fn();
        const wrap = buildFeedView([], { filters: filters(), onFiltersChange: vi.fn(), onRefresh });

        wrap.querySelector<HTMLButtonElement>(".af-feed-refresh")!.click();

        expect(onRefresh).toHaveBeenCalledOnce();
    });

    it("follow reports both directions and shows its state", () => {
        const onFollowChange = vi.fn();
        const wrap = buildFeedView([], {
            filters: filters(),
            onFiltersChange: vi.fn(),
            onFollowChange,
        });
        const btn = wrap.querySelector<HTMLButtonElement>(".af-feed-follow")!;

        btn.click();
        expect(onFollowChange).toHaveBeenLastCalledWith(true);
        expect(btn.textContent).toContain("on");

        btn.click();
        expect(onFollowChange).toHaveBeenLastCalledWith(false);
        expect(btn.textContent).toContain("off");
    });

    it("starts on when the caller says it already is", () => {
        // The view is rebuilt on every tab switch; a control that forgot would
        // read "off" while the timer was still running.
        const wrap = buildFeedView([], {
            filters: filters(),
            onFiltersChange: vi.fn(),
            onFollowChange: vi.fn(),
            following: true,
        });

        expect(wrap.querySelector(".af-feed-follow")!.textContent).toContain("on");
    });
});

describe("appending filters only the new row", () => {
    /**
     * The append path used to re-run the whole-list filter, reading
     * `textContent` on every row — O(n²) over a session, and `follow` makes it
     * recur. It now judges just the row it added, so these pin the two
     * consequences of that: rows already on screen are left alone, and the
     * empty-state placeholder still resolves correctly.
     *
     * Deliberately not a timing assertion — a wall-clock bound in CI flakes on
     * a busy runner and teaches nobody anything when it does.
     */
    function feedWith(...texts: string[]): { root: HTMLElement; f: FeedFilters } {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"><div class="af-feed-empty">No events yet.</div></div>`;
        const f = filters();
        texts.forEach((t, i) => appendFeedItemToView(root, log({ ts: 1_700_000_000 + i, text: t }), f));
        return { root, f };
    }

    it("leaves an already-hidden row hidden", () => {
        const { root, f } = feedWith("keep me");
        const first = root.querySelector<HTMLElement>(".af-feed-item")!;
        first.hidden = true; // as a filter change would have left it

        appendFeedItemToView(root, log({ ts: 1_700_000_100, text: "newcomer" }), f);

        expect(first.hidden).toBe(true);
    });

    it("retires the placeholder when the new row is visible", () => {
        const { root } = feedWith("first");

        expect(root.querySelector<HTMLElement>(".af-feed-empty")!.hidden).toBe(true);
    });

    it("leaves the placeholder alone when the new row is filtered out", () => {
        const root = document.createElement("div");
        root.innerHTML = `<div id="af-feed-view"><div class="af-feed-empty">No events yet.</div></div>`;

        appendFeedItemToView(root, log({ text: "hidden by search" }), filters({ search: "nothing" }));

        // Still empty as far as the reader is concerned, so the placeholder stays.
        expect(root.querySelector<HTMLElement>(".af-feed-empty")!.hidden).toBe(false);
    });
});
