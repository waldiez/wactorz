/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Tells a polling caller when its failures have stopped being routine.
 *
 * A single failed poll is unremarkable — the dev server is often simply not
 * running — so the first few are logged at debug and nothing is said. Once they
 * stack up the backend is genuinely gone, which is worth saying out loud, but
 * only once: the poll repeats on a timer and a message per attempt would bury
 * the signal it exists to provide.
 */

/** Consecutive failures before an outage is worth reporting. */
export const OUTAGE_THRESHOLD = 3;

export interface OutageTracker {
    /** Record a failed call; true exactly once per outage, when it's worth reporting. */
    recordFailure(): boolean;
    /** Record a successful call, ending any outage in progress. */
    recordSuccess(): void;
}

export function createOutageTracker(threshold: number = OUTAGE_THRESHOLD): OutageTracker {
    let consecutive = 0;
    let reported = false;
    return {
        recordFailure(): boolean {
            consecutive++;
            if (consecutive >= threshold && !reported) {
                reported = true;
                return true;
            }
            return false;
        },
        recordSuccess(): void {
            consecutive = 0;
            reported = false;
        },
    };
}
