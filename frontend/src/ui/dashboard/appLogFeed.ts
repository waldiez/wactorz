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
import { fetchAppLogs, toEntry } from "./appLogs";
import { listen } from "../../events";

/** How often "follow" re-fetches. Slow enough that an idle dashboard is not a
 *  load generator, fast enough to read as live. */
export const FOLLOW_INTERVAL_MS = 5000;

/**
 * Most records kept in memory.
 *
 * `load()` replaces the list with a bounded response, but pushed records are
 * appended — so without a cap a dashboard left open overnight accumulates every
 * line the server logged. The view itself trims to its own limit; this is the
 * array behind it.
 */
export const MAX_ENTRIES = 600;

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
    private _unlisten: ((e: Event) => void) | null = null;

    constructor(private host: AppLogHost) {}

    /**
     * Subscribe to records the server pushes as it writes them.
     *
     * Paired with `unwire`, and called from the host's own event wiring rather
     * than from the constructor: subscribing once at construction cannot be
     * undone and redone, so a dashboard that is hidden and shown again would
     * come back with the push silently dead — and the periodic fetch would mask
     * it, leaving a view that looks live and lags by five seconds.
     *
     * Idempotent, so wiring twice does not stack two listeners.
     *
     * Records keep accumulating while the activity view is closed — `receive`
     * only touches the DOM when it is open — so returning to it shows what
     * happened meanwhile.
     */
    wire(): void {
        this._unlisten ??= listen("af-app-log", d => this.receive(d?.entries ?? []));
    }

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
            this._entries = entries.slice(-MAX_ENTRIES);
            entries
                .filter(e => !known.has(feedKey(e)))
                .forEach(e => appendFeedItemToView(this.host.root, e, this.host.filters()));
        });
    }

    /**
     * Fold records pushed by the server into the rows on screen.
     *
     * Deduplicated against what is already held, exactly as `load()` is: a
     * record can arrive twice — pushed as it happens, then again in the next
     * fetch — and `feedKey` is what makes that harmless rather than a double
     * row. That overlap is deliberate; the push keeps the view live, the fetch
     * is what recovers it after a disconnection.
     */
    receive(raw: readonly unknown[]): void {
        const entries = raw.map(toEntry).filter((e): e is AppLogItem => e !== null);
        if (!entries.length) {
            return;
        }
        const known = new Set(this._entries.map(feedKey));
        const fresh = entries.filter(e => !known.has(feedKey(e)));
        if (!fresh.length) {
            return;
        }
        this._entries = [...this._entries, ...fresh].slice(-MAX_ENTRIES);
        if (this.host.isFeedView()) {
            fresh.forEach(e => appendFeedItemToView(this.host.root, e, this.host.filters()));
        }
    }

    /** Start or stop re-fetching. Idempotent — never stacks two timers. */
    setFollowing(on: boolean): void {
        this._following = on;
        this.stop();
        if (on) {
            this._timer = setInterval(() => this.load(), FOLLOW_INTERVAL_MS);
        }
    }

    /** Drop the push subscription. Safe to call when there is none. */
    unwire(): void {
        if (this._unlisten) {
            document.removeEventListener("af-app-log", this._unlisten);
            this._unlisten = null;
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
