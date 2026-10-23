/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What the overview fetches on a timer rather than hears live: each agent's
 * activity over the last hour for its card, and for each node its manifest and
 * its last hour of CPU and free memory.
 *
 * One request covers every agent's card. Nodes are few, so each has its own.
 * Nothing is fetched while the page is hidden or another view is showing.
 */
import { fetchAgentsField, fetchHistory, fetchNodes, type NodeListing } from "./history";
import type { NodeTrend } from "./nodeCard";
import { levels, perMinute, samplesOf, type Point } from "./trend";

/** How often the overview's trends are refreshed: about as often as the history is sampled. */
export const REFRESH_MS = 60_000;

/** The span of a card's trend, in hours. */
export const CARD_HOURS = 1;

/** What the poller asks of the dashboard. */
export interface OverviewDataHost {
    /** Whether the overview is the view showing. */
    isOverview(): boolean;
    /** The names of the remote nodes the dashboard has heard from. */
    nodeNames(): string[];
    /** Something was fetched; repaint what draws from it. */
    onUpdate(): void;
}

/** The readers the poller uses; replaced in tests. */
export interface OverviewReaders {
    agentsField: typeof fetchAgentsField;
    nodes: typeof fetchNodes;
    history: typeof fetchHistory;
}

const READERS: OverviewReaders = { agentsField: fetchAgentsField, nodes: fetchNodes, history: fetchHistory };

/** The overview's fetched data, and the timer that keeps it fresh. */
export class OverviewData {
    /** Each agent's messages per minute over the last hour, by name. */
    readonly agentRates = new Map<string, Point[]>();
    /** Each node's trend over the last hour, by name. */
    readonly nodeTrends = new Map<string, NodeTrend>();
    /** Each node as the server lists it, manifest included, by name. */
    readonly listings = new Map<string, NodeListing>();
    private _timer: ReturnType<typeof setInterval> | null = null;

    constructor(
        private readonly _host: OverviewDataHost,
        private readonly _read: OverviewReaders = READERS,
    ) {}

    /** Fetch now, then every `REFRESH_MS` while the overview is showing and the page is visible. */
    start(): void {
        this.stop();
        void this.refresh();
        this._timer = setInterval(() => {
            if (!document.hidden && this._host.isOverview()) {
                void this.refresh();
            }
        }, REFRESH_MS);
    }

    /** Stop refreshing. */
    stop(): void {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
    }

    /** Fetch everything once, keeping what a failed request would have replaced. */
    async refresh(): Promise<void> {
        const [rates, listings] = await Promise.all([
            this._read.agentsField("messages_processed", CARD_HOURS),
            this._read.nodes(),
        ]);
        if (rates) {
            this.agentRates.clear();
            for (const [name, samples] of rates) {
                this.agentRates.set(name, perMinute(samples));
            }
        }
        if (listings) {
            this.listings.clear();
            for (const listing of listings) {
                this.listings.set(listing.node, listing);
            }
        }
        const names = new Set([...this.listings.keys(), ...this._host.nodeNames()]);
        await Promise.all(
            [...names].map(async name => {
                const rows = await this._read.history("nodes", name, CARD_HOURS);
                if (rows) {
                    this.nodeTrends.set(name, {
                        cpu: levels(samplesOf(rows, "cpu_pct")),
                        free: levels(samplesOf(rows, "mem_free_mb")),
                    });
                }
            }),
        );
        this._host.onUpdate();
    }
}
