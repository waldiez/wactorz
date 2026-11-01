/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { defineConfig } from "vitest/config";

// Coverage floors — ratchet up as coverage grows, never down. Lines, statements
// and functions share TARGET; branches trail (defensive guards + flag-gated
// paths cover slower), so they keep a small offset.
const TARGET = 97;
const BRANCHES_FLOOR = TARGET - 5;

export default defineConfig({
    // The test-id contract (testids.test.ts) reads the end-to-end harness, which
    // sits outside the frontend; this lets Vite read that folder and no other.
    server: { fs: { allow: [".", "../e2e/harness"] } },
    test: {
        environment: "happy-dom",
        pool: "vmThreads",
        globals: true,
        include: ["src/**/*.test.ts"],
        setupFiles: ["src/__tests__/setup.ts"],
        coverage: {
            provider: "v8",
            reporter: ["text", "lcov", "html"],
            include: ["src/**/*.ts"],
            // Nothing is excluded. The composition root (app.ts) is covered by
            // app.test.ts, which fakes the transports and drives each
            // registered handler; its decision/transform logic lives in tested
            // modules (agents/mapping, agents/deletionGuard, ui/haFeed).
            exclude: [],
            // Floors derived from TARGET (see top of file). CI fails below these.
            thresholds: {
                lines: TARGET,
                statements: TARGET,
                functions: TARGET,
                branches: BRANCHES_FLOOR,
            },
        },
    },
});
