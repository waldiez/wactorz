/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Asking the server to clear what it holds, `POST /api/reset`.
 */
import { apiUrl, posting, requestJson, type Answer } from "./api";

/** How long a reset may take: it stops and restarts agents, then waits on the nodes. */
export const RESET_TIMEOUT_MS = 120_000;

/** What a finished reset reports. */
export interface ResetDone {
    /** What each node said, by node name. */
    nodes?: Record<string, string>;
}

/** Clear `scope`, for one `agent` or for everything. */
export function requestReset(scope: string, agent: string | null): Promise<Answer<ResetDone | null>> {
    return requestJson<ResetDone | null>(
        apiUrl("/api/reset"),
        posting(agent ? { scope, agent } : { scope }),
        RESET_TIMEOUT_MS,
    );
}
