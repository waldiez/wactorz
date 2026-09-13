/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wire contracts for the monitor server's `/ws` endpoint — the data shapes the
 * server broadcasts (state patches, snapshot totals, log-feed entries). The
 * `WSClient` consumes these and the mappers in `agents/mapping.ts` turn them
 * into UI models. Callback/handler signatures stay with the client.
 */

/** One agent entry as the server includes it in state-patch messages. */
export type StatePatchAgent = {
    agent_id: string;
    name?: string;
    state?: string;
    status?: string;
    protected?: boolean;
    essential?: boolean;
    messages_processed?: number;
    cost_usd?: number;
    uptime?: number;
    cpu?: number;
    mem?: number;
    task?: string;
    agent_type?: string;
    /** Set for remote-runner agents — the node name; marks the agent as remote. */
    node?: string;
};

/** Snapshot-level totals computed by the backend (includes historical/deleted agents). */
export type SnapshotStats = {
    totalCostUsd?: number;
    totalMessages?: number;
};

/** One MQTT-derived event entry from the server's in-memory log_feed. */
export interface LogFeedItem {
    type: string;
    agent_id?: string;
    name?: string;
    agentName?: string;
    message?: string;
    text?: string;
    timestamp?: number;
    status?: Record<string, unknown>;
    severity?: string;
    agentType?: string;
    agent_type?: string;
    command?: string;
}
