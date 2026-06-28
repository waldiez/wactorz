/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { defineConfig } from "vitest/config";

// Single knob for the coverage floor (lines & statements). Ratchet it up as
// coverage grows — never down. Functions and branches trail and get their own
// offsets: branches especially cover slower (defensive guards + flag-gated
// paths), so when you raise TARGET you may need to widen their offset too.
const TARGET = 95;
const FUNCTIONS_FLOOR = TARGET - 3; // 92
const BRANCHES_FLOOR = TARGET - 13; // 82

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
            // Composition root only: main.ts constructs singletons and wires
            // transports (MQTT/WS/DOM) to the store + feed. Its logic lives in
            // tested modules (agents/mapping, agents/deletionGuard, ui/haFeed);
            // the rest is declarative wiring (not for unit-testing).
            exclude: ["src/main.ts"],
            // Floors derived from TARGET (see top of file). CI fails below these.
            thresholds: {
                lines: TARGET,
                statements: TARGET,
                functions: FUNCTIONS_FLOOR,
                branches: BRANCHES_FLOOR,
            },
        },
    },
});
