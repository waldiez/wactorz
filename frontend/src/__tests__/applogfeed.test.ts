/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The application-log controller: what it fetches, and when it stops.
 *
 * The timer is the part worth pinning. A poll that outlives the view keeps
 * asking for something nobody is looking at, and a second one stacked on the
 * first is invisible until the request rate doubles.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AppLogFeed, FOLLOW_INTERVAL_MS } from "../ui/dashboard/appLogFeed";
import { DEFAULT_FILTERS } from "../ui/dashboard/feedView";

const record = (text: string, ts = 1_700_000_000) => ({
    source: "app",
    ts,
    level: "INFO",
    origin: "wactorz.web",
    text,
});

function serve(...texts: string[]): void {
    vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({
            ok: true,
            json: async () => ({ entries: texts.map((t, i) => record(t, 1_700_000_000 + i)) }),
        })),
    );
}

function host(onScreen = true) {
    const root = document.createElement("div");
    root.innerHTML = `<div id="af-feed-view"></div>`;
    document.body.appendChild(root);
    return {
        root,
        isFeedView: () => onScreen,
        filters: () => ({ ...DEFAULT_FILTERS, source: "all" as const, hideHeartbeats: false }),
    };
}

const rows = (h: { root: HTMLElement }) => h.root.querySelectorAll(".af-feed-item").length;

describe("AppLogFeed.load", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });
    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it("renders what it fetched", async () => {
        const h = host();
        serve("one", "two");

        await new AppLogFeed(h).load();
        await vi.waitFor(() => expect(rows(h)).toBe(2));
    });

    it("appends only records not already on screen", async () => {
        // A rebuild draws `entries`, so re-appending the whole response would
        // double every record on each refresh.
        const h = host();
        const feed = new AppLogFeed(h);
        serve("one", "two");
        feed.load();
        await vi.waitFor(() => expect(rows(h)).toBe(2));

        serve("one", "two", "three");
        feed.load();

        await vi.waitFor(() => expect(rows(h)).toBe(3));
    });

    it("draws nothing when the view has moved on", async () => {
        const h = host(false);
        serve("one");

        new AppLogFeed(h).load();

        await new Promise(r => setTimeout(r, 0));
        expect(rows(h)).toBe(0);
    });
});

describe("following", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        serve("one");
    });
    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it("re-fetches on an interval while on", () => {
        const feed = new AppLogFeed(host());
        feed.setFollowing(true);
        vi.mocked(fetch).mockClear();

        vi.advanceTimersByTime(FOLLOW_INTERVAL_MS * 3);

        expect(vi.mocked(fetch).mock.calls.length).toBe(3);
    });

    it("stops when switched off", () => {
        const feed = new AppLogFeed(host());
        feed.setFollowing(true);
        feed.setFollowing(false);
        vi.mocked(fetch).mockClear();

        vi.advanceTimersByTime(FOLLOW_INTERVAL_MS * 3);

        expect(fetch).not.toHaveBeenCalled();
    });

    it("never stacks two timers", () => {
        // Switching on twice must not double the request rate — the second
        // timer would be invisible until someone counted requests.
        const feed = new AppLogFeed(host());
        feed.setFollowing(true);
        feed.setFollowing(true);
        vi.mocked(fetch).mockClear();

        vi.advanceTimersByTime(FOLLOW_INTERVAL_MS);

        expect(vi.mocked(fetch).mock.calls.length).toBe(1);
    });

    it("stop() drops the timer but keeps the preference", () => {
        const feed = new AppLogFeed(host());
        feed.setFollowing(true);

        feed.stop();

        expect(feed.armed).toBe(false);
        expect(feed.following).toBe(true); // returning to the view resumes it
    });

    it("resume() re-arms once, not on every render", () => {
        const feed = new AppLogFeed(host());
        feed.setFollowing(true);
        feed.stop();

        feed.resume();
        feed.resume();
        vi.mocked(fetch).mockClear();
        vi.advanceTimersByTime(FOLLOW_INTERVAL_MS);

        expect(vi.mocked(fetch).mock.calls.length).toBe(1);
    });

    it("resume() does nothing when following was never on", () => {
        const feed = new AppLogFeed(host());

        feed.resume();

        expect(feed.armed).toBe(false);
    });
});
