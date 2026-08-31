/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The activity view's model: agent events and application-log records together.
 *
 * The view draws one list from two sources that arrive by different routes —
 * agent events over `af-feed-push`, log records fetched and pushed by
 * `AppLogFeed` — and everything downstream of that (dedup, the cap, the
 * toolbar's filters) applies to both. Keeping the two halves in one place is
 * what lets the view stay a rendering function.
 *
 * Filters live here rather than in the view because the view is rebuilt on
 * every tab switch, and state owned by it would drop a search mid-investigation.
 */
import type { FeedItem } from "../../types/feed";
import { AppLogFeed } from "./appLogFeed";
import { appendFeedItemToView, buildFeedView, DEFAULT_FILTERS, feedKey, type FeedFilters } from "./feedView";

/**
 * Most agent events held.
 *
 * The view trims what it draws; this bounds what is remembered, so a long
 * session does not accumulate every event the server ever sent.
 */
export const MAX_ITEMS = 500;

export interface ActivityHost {
    readonly root: HTMLElement;
    /** Whether the activity view is the one on screen. */
    isFeedView(): boolean;
}

export class ActivityFeed {
    private _items: FeedItem[] = [];
    /** Content keys of held items, for O(1) dedup across the sources. */
    private _keys = new Set<string>();
    private _filters: FeedFilters = { ...DEFAULT_FILTERS };
    readonly logs: AppLogFeed;

    constructor(private host: ActivityHost) {
        this.logs = new AppLogFeed({
            root: host.root,
            isFeedView: () => host.isFeedView(),
            filters: () => this._filters,
        });
    }

    /** How many agent events are held — the overview's "feed events" figure. */
    get count(): number {
        return this._items.length;
    }

    /**
     * Take one agent event, unless it is one already held.
     *
     * The same event arrives from several sources — the SQLite seed, the
     * WebSocket `log_feed` replay, live chat — so without the key check the
     * feed doubles up and reorders itself on the next rebuild.
     */
    push(item: FeedItem): void {
        const key = feedKey(item);
        if (this._keys.has(key)) {
            return;
        }
        this._keys.add(key);
        this._items.push(item);
        if (this._items.length > MAX_ITEMS) {
            const dropped = this._items.shift();
            if (dropped) {
                this._keys.delete(feedKey(dropped));
            }
        }
        if (this.host.isFeedView()) {
            appendFeedItemToView(this.host.root, item, this._filters);
        }
    }

    /** Forget every agent event. The log half keeps its own records. */
    clear(): void {
        this._items = [];
        this._keys.clear();
    }

    /** Build the activity view over both sources, newest handling intact. */
    build(): HTMLElement {
        return buildFeedView([...this._items, ...this.logs.entries], {
            filters: this._filters,
            onRefresh: () => this.logs.load(),
            following: this.logs.following,
            onFollowChange: on => this.logs.setFollowing(on),
            onFiltersChange: f => {
                this._filters = f;
            },
        });
    }

    /** Start listening for pushed log records. Idempotent. */
    wire(): void {
        this.logs.wire();
    }

    /** Stop following and drop the push subscription. */
    release(): void {
        this.logs.stop();
        this.logs.unwire();
    }
}
