/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { defineConfig } from "vitest/config";

// Coverage floors — ratchet up as coverage grows, never down. Lines, statements
// and functions share TARGET; branches trail (defensive guards + flag-gated
// paths cover slower), so they keep a small offset.
const TARGET = 95;
const BRANCHES_FLOOR = TARGET - 10; // 85

export default defineConfig({
    test: {
        environment: "happy-dom",
        globals: true,
        include: ["src/**/*.test.ts"],
        setupFiles: ["src/__tests__/setup.ts"],
        coverage: {
            provider: "v8",
            reporter: ["text", "lcov", "html"],
            include: ["src/**/*.ts"],
            // Nothing is excluded. The composition root (main.ts) is covered by
            // main-bootstrap.test.ts, which mocks the transports and drives each
            // registered handler; its decision/transform logic lives in tested
            // modules (agents/mapping, agents/deletionGuard, ui/haFeed, ui/dashboard/haConfig).
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
