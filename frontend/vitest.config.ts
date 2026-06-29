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
            // Composition root only: main.ts constructs singletons, derives
            // same-origin URLs and wires transports (MQTT/WS/DOM) to the store +
            // feed. The decisions it used to inline now live in tested modules
            // (agents/feedEvents, agents/mapping, agents/deletionGuard, ui/haFeed),
            // so the handlers are thin delegators — what remains is declarative
            // wiring + import-time bootstrap, not unit-testable in isolation.
            exclude: ["src/main.ts"],
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
