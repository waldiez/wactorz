/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Keeping the agents in step with the server's list of them.
 *
 * Events keep the store current, but they can be missed: the page was closed,
 * or the agents started before the browser connected. So the list is read too,
 * at start, on a timer and after a stop, and the store is reconciled with it.
 */
import { apiUrl, requestJson } from "../io/api";
import { log } from "../io/logger";
import { createOutageTracker } from "../io/outage";
import type { AgentInfo } from "../types/agent";
import type { AgentStore } from "./AgentStore";
import type { DeletionGuard } from "./deletionGuard";
import { reconcileActorList } from "./mapping";

/** How long a read of the list waits for an answer. */
const ACTOR_POLL_TIMEOUT_MS = 10_000;

/** Reads the server's list of agents into the store, one read at a time. */
export class ActorSync {
    private _reading = false;
    private _seeded = false;
    private readonly _outage = createOutageTracker();

    constructor(
        private readonly store: Pick<AgentStore, "reconcileAgents">,
        private readonly isDeleted: DeletionGuard["isDeleted"],
    ) {}

    /** Read the list, unless a read is under way. */
    async refresh(): Promise<void> {
        if (this._reading) {
            return;
        }
        this._reading = true;
        // Bound the request so a hung fetch can't pin the latch and wedge every
        // future refresh, and shorter than most: the next poll asks again anyway.
        const got = await requestJson<AgentInfo[]>(apiUrl("/api/actors"), {}, ACTOR_POLL_TIMEOUT_MS);
        this._reading = false;
        try {
            if (!got.ok) {
                throw new Error(got.error);
            }
            this._outage.recordSuccess();
            this.store.reconcileAgents(reconcileActorList(got.value, this.isDeleted));
            log.info(`[Dashboard] reconciled ${got.value.length} live actors from REST`);
        } catch (err) {
            // Dev mode without a running server is expected; log at debug so a
            // genuine backend failure still leaves a trace. A run of them is not
            // routine, so say that once, at a level that survives a production
            // build.
            log.debug("[Dashboard] live actor refresh failed:", err);
            if (this._outage.recordFailure()) {
                log.warn("[Dashboard] backend unreachable — live actor list is stale");
            }
        }
    }

    /** Read the list the first time the page goes live. Startup spawn events are
     *  published before the browser connects; reconnects must not read for that. */
    async seedOnce(): Promise<void> {
        if (!this._seeded) {
            this._seeded = true;
            await this.refresh();
        }
    }
}
