/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { createOutageTracker, OUTAGE_THRESHOLD } from "../io/outage";

describe("createOutageTracker", () => {
    it("stays quiet about the first failures", () => {
        const t = createOutageTracker();
        for (let i = 0; i < OUTAGE_THRESHOLD - 1; i++) {
            expect(t.recordFailure()).toBe(false);
        }
    });

    it("reports once when failures reach the threshold", () => {
        const t = createOutageTracker();
        for (let i = 0; i < OUTAGE_THRESHOLD - 1; i++) {
            t.recordFailure();
        }
        expect(t.recordFailure()).toBe(true);
    });

    it("does not repeat itself while the outage continues", () => {
        const t = createOutageTracker();
        let reported = 0;
        for (let i = 0; i < OUTAGE_THRESHOLD * 5; i++) {
            if (t.recordFailure()) {
                reported++;
            }
        }
        expect(reported).toBe(1);
    });

    it("reports again after recovering and failing anew", () => {
        const t = createOutageTracker();
        const runToThreshold = () => {
            let reported = false;
            for (let i = 0; i < OUTAGE_THRESHOLD; i++) {
                reported = t.recordFailure() || reported;
            }
            return reported;
        };
        expect(runToThreshold()).toBe(true);
        t.recordSuccess();
        expect(runToThreshold()).toBe(true);
    });

    it("forgets a partial run of failures once a call succeeds", () => {
        const t = createOutageTracker();
        for (let i = 0; i < OUTAGE_THRESHOLD - 1; i++) {
            t.recordFailure();
        }
        t.recordSuccess();
        expect(t.recordFailure()).toBe(false);
    });
});
