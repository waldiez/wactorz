/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * An agent's state as the server reports it, read into an {@link AgentState}.
 *
 * The state arrives by several paths: the REST list, the WS state patch,
 * heartbeats and status messages. All of them read it here, so an agent
 * cannot look failed by one path and running by another.
 */
import type { AgentState } from "../types/agent";

/**
 * Read an untrusted state. The server's own words are `idle` (built, not yet
 * started, so starting here), `running`, `stopped` and `failed`; a failure may
 * also come as `{failed: reason}`. Anything else, a missing state included,
 * reads as running, so an agent the server lists without a state still counts
 * as live.
 */
export function parseAgentState(v: unknown): AgentState {
    if (v === "running" || v === "stopped" || v === "initializing") {
        return v;
    }
    if (v === "idle") {
        return "initializing";
    }
    if (v === "failed") {
        return { failed: "" };
    }
    if (v !== null && typeof v === "object" && typeof (v as { failed?: unknown }).failed === "string") {
        return { failed: (v as { failed: string }).failed };
    }
    return "running";
}
