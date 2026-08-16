/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActivityFeed, MAX_ITEMS } from "../ui/dashboard/activityFeed";
import type { FeedItem } from "../types/feed";

const record = (text: string, ts = 1_700_000_000) => ({
    source: "app",
    ts,
    level: "INFO",
    origin: "wactorz.web",
    text,
});

/** What the server push looks like on the wire. */
const push = (...texts: string[]) =>
    document.dispatchEvent(new CustomEvent("af-app-log", { detail: { entries: texts.map(t => record(t)) } }));

// ActivityFeed is the activity view's model: agent events and log records held
// together, with the dedup, cap and filters that apply to both. The view is
// rebuilt on every tab switch, so what is worth pinning here is what survives
// that — the held items, the bound on them, and the filters the rebuild reads.

function item(over: Partial<FeedItem> = {}): FeedItem {
    return { type: "chat", label: "hi", agentName: "main", timestamp: 1000, ...over };
}

/** A host whose activity view is open, unless told otherwise. */
function host(isFeedView = true): { root: HTMLElement; isFeedView: () => boolean } {
    const root = document.createElement("div");
    document.body.appendChild(root);
    return { root, isFeedView: () => isFeedView };
}

describe("holding agent events", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("counts what it holds", () => {
        const feed = new ActivityFeed(host());

        feed.push(item());

        expect(feed.count).toBe(1);
    });

    it("takes the same event only once", () => {
        // It arrives from the SQLite seed, the WebSocket replay and live chat,
        // so without the key check the feed doubles up on every rebuild.
        const feed = new ActivityFeed(host());

        feed.push(item());
        feed.push(item());

        expect(feed.count).toBe(1);
    });

    it("forgets everything when cleared", () => {
        const feed = new ActivityFeed(host());
        feed.push(item());

        feed.clear();

        expect(feed.count).toBe(0);
    });

    it("takes an event again after a clear", () => {
        // The dedup keys have to go with the items; keeping them would make a
        // cleared feed refuse the very events that would refill it.
        const feed = new ActivityFeed(host());
        feed.push(item());
        feed.clear();

        feed.push(item());

        expect(feed.count).toBe(1);
    });
});

describe("the cap on held events", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    /** One more event than the cap allows, each distinct. */
    const overfill = (feed: ActivityFeed) => {
        for (let i = 0; i <= MAX_ITEMS; i++) {
            feed.push(item({ label: `event ${i}`, timestamp: i }));
        }
    };

    it("stops growing at the cap", () => {
        // A long session would otherwise accumulate every event the server ever
        // sent, for a view that only ever draws the newest of them.
        const feed = new ActivityFeed(host());

        overfill(feed);

        expect(feed.count).toBe(MAX_ITEMS);
    });

    it("drops the oldest rather than refusing the newest", () => {
        const feed = new ActivityFeed(host(false));

        overfill(feed);
        const labels = [...feed.build().querySelectorAll("*")].map(n => n.textContent ?? "");

        expect(labels.some(text => text.includes(`event ${MAX_ITEMS}`))).toBe(true);
    });

    it("lets an evicted event back in", () => {
        // The dedup key of a dropped item has to be dropped with it. If it were
        // not, an event pushed out by the cap could never return, and the feed
        // would silently swallow it for the rest of the session.
        const feed = new ActivityFeed(host());
        const oldest = item({ label: "event 0", timestamp: 0 });
        overfill(feed);

        feed.push(oldest);

        expect(feed.count).toBe(MAX_ITEMS);
        expect(feed.build().textContent).toContain("event 0");
    });
});

describe("drawing into an open view", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("appends to the view while the activity tab is open", () => {
        const open = host(true);
        const feed = new ActivityFeed(open);
        open.root.appendChild(feed.build());

        feed.push(item({ label: "arrived while watching" }));

        expect(open.root.textContent).toContain("arrived while watching");
    });

    it("holds an event that arrives while the tab is closed", () => {
        // Kept rather than drawn: the view is rebuilt on the next tab switch,
        // and `build` is what puts the held events on screen.
        const closed = host(false);
        const feed = new ActivityFeed(closed);

        feed.push(item({ label: "arrived while away" }));

        expect(feed.count).toBe(1);
        expect(feed.build().textContent).toContain("arrived while away");
    });
});

describe("filters outliving the view", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    const searchBox = (root: HTMLElement) => root.querySelector<HTMLInputElement>("input.af-feed-search");

    it("keeps a search across a rebuild", () => {
        // The view is thrown away on every tab switch, so filters owned by it
        // would drop a search mid-investigation. Asserted on the rebuilt
        // toolbar rather than on the rows, because filtering hides rows in
        // place — the text is still there, which would make a row check pass
        // whether or not the filter survived.
        const open = host();
        const feed = new ActivityFeed(open);
        feed.push(item({ label: "keep me" }));
        const view = feed.build();
        open.root.appendChild(view);
        const box = searchBox(view);
        expect(box).not.toBeNull();

        box!.value = "keep";
        box!.dispatchEvent(new Event("input", { bubbles: true }));

        expect(searchBox(feed.build())?.value).toBe("keep");
    });

    it("keeps a source filter across a rebuild", () => {
        const open = host();
        const feed = new ActivityFeed(open);
        const view = feed.build();
        open.root.appendChild(view);
        const source = view.querySelector<HTMLSelectElement>("select");
        expect(source).not.toBeNull();

        source!.value = "app";
        source!.dispatchEvent(new Event("change", { bubbles: true }));

        expect(feed.build().querySelector<HTMLSelectElement>("select")?.value).toBe("app");
    });

    it("passes the follow toggle down to the log half", () => {
        // Following is the log half's own state; the toolbar that toggles it
        // belongs to the view, which is rebuilt from here.
        const feed = new ActivityFeed(host());
        const setFollowing = vi.spyOn(feed.logs, "setFollowing");
        const view = feed.build();
        const follow = view.querySelector<HTMLButtonElement>("button.af-feed-follow");
        expect(follow).not.toBeNull();

        follow!.click();

        expect(setFollowing).toHaveBeenCalledWith(true);
    });

    it("asks the log half to re-fetch when the toolbar refreshes", () => {
        const feed = new ActivityFeed(host());
        const load = vi.spyOn(feed.logs, "load").mockResolvedValue(undefined);
        const view = feed.build();
        const refresh = [...view.querySelectorAll("button")].find(b =>
            (b.title + b.textContent).toLowerCase().includes("refresh"),
        );
        expect(refresh).toBeDefined();

        refresh!.click();

        expect(load).toHaveBeenCalled();
    });
});

describe("the log half", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("wires the push subscription through", () => {
        const feed = new ActivityFeed(host());
        const wire = vi.spyOn(feed.logs, "wire");

        feed.wire();

        expect(wire).toHaveBeenCalled();
    });

    it("stops following and drops the subscription on release", () => {
        const feed = new ActivityFeed(host());
        const stop = vi.spyOn(feed.logs, "stop");
        const unwire = vi.spyOn(feed.logs, "unwire");
        feed.wire();

        feed.release();

        expect(stop).toHaveBeenCalled();
        expect(unwire).toHaveBeenCalled();
    });

    it("takes pushed records once wired", () => {
        const feed = new ActivityFeed(host());
        feed.wire();

        push("from the log");

        expect(feed.logs.entries).toHaveLength(1);
    });

    it("draws both sources into one list", () => {
        const feed = new ActivityFeed(host());
        feed.wire();
        feed.push(item({ label: "an agent event" }));
        push("a log record");

        const view = feed.build();

        expect(view.textContent).toContain("an agent event");
        expect(view.textContent).toContain("a log record");
    });

    it("a cleared feed keeps the log records", () => {
        // `clear` is the agent-event control; the log half holds its own.
        const feed = new ActivityFeed(host());
        feed.wire();
        push("kept");

        feed.clear();

        expect(feed.logs.entries).toHaveLength(1);
    });
});
