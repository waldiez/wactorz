/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The spend limit, as `GET /api/cost` reports it.
 */
import { apiUrl, requestJson } from "./api";

/** The configured limit and the spend against it. */
export interface CostLimitInfo {
    /** The limit, in US dollars. */
    limit_usd?: number;
    /** The period it covers. */
    period?: string;
    /** What has been spent in that period, in US dollars. */
    spend_usd?: number;
    /** Server-computed progress fields (present once a limit is configured). */
    pct_used?: number;
    /** Whether spend is close to the limit. */
    warning?: boolean;
    /** Whether spend has reached the limit. */
    limit_reached?: boolean;
}

/** The spend limit, or null when the server could not say: it may not be ready yet. */
export async function readCostLimit(): Promise<CostLimitInfo | null> {
    const got = await requestJson<CostLimitInfo | null>(apiUrl("/api/cost"));
    return got.ok ? got.value : null;
}
