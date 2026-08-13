/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The application-log half of the activity view, as its own controller.
 *
 * Owns the records fetched from `/api/logs`, the "follow" timer, and the rule
 * that neither outlives the view. Separate from `CardDashboard` for the same
 * reason the chat and metrics concerns are: it has state with a lifecycle, and
 * a timer nobody stops is the classic way a dashboard keeps working after the
 * page has moved on.
 */
import type { AppLogItem, FeedItem } from "../../types/feed";
import { appendFeedItemToView, feedKey, type FeedFilters } from "./feedView";
import { fetchAppLogs } from "./appLogs";

/** How often "follow" re-fetches. Slow enough that an idle dashboard is not a
 *  load generator, fast enough to read as live. */
export const FOLLOW_INTERVAL_MS = 5000;

export interface AppLogHost {
    readonly root: HTMLElement;
    /** Whether the activity view is the one on screen. */
    isFeedView(): boolean;
    /** The toolbar's current state, which decides what a new row does. */
    filters(): FeedFilters;
}

export class AppLogFeed {
    private _entries: AppLogItem[] = [];
    private _timer: ReturnType<typeof setInterval> | null = null;
    private _following = false;

    constructor(private host: AppLogHost) {}

    /** The records to render alongside agent activity on the next build. */
    get entries(): readonly (AppLogItem | FeedItem)[] {
        return this._entries;
    }

    get following(): boolean {
        return this._following;
    }

    /** Whether a re-fetch timer is currently armed. */
    get armed(): boolean {
        return this._timer !== null;
    }

    /**
     * Fetch and fold the log into the rows already on screen.
     *
     * Only entries not already rendered are appended: a rebuild draws
     * `entries`, so appending the whole response would double every record
     * each time the view is opened or refreshed.
     */
    load(): void {
        void fetchAppLogs().then(entries => {
            if (!entries?.length || !this.host.isFeedView()) {
                return;
            }
            const known = new Set(this._entries.map(feedKey));
            this._entries = entries;
            entries
                .filter(e => !known.has(feedKey(e)))
                .forEach(e => appendFeedItemToView(this.host.root, e, this.host.filters()));
        });
    }

    /** Start or stop re-fetching. Idempotent — never stacks two timers. */
    setFollowing(on: boolean): void {
        this._following = on;
        this.stop();
        if (on) {
            this._timer = setInterval(() => this.load(), FOLLOW_INTERVAL_MS);
        }
    }

    /** Drop the timer, keeping the preference so returning resumes it. */
    stop(): void {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
    }

    /** Re-arm on returning to the view — but only if it is not already armed,
     *  since the view re-renders on unrelated updates and restarting the
     *  interval each time would mean it never fires. */
    resume(): void {
        if (this._following && !this._timer) {
            this.setFollowing(true);
        }
    }
}
