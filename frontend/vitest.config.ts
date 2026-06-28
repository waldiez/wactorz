/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { defineConfig } from "vitest/config";

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
            exclude: [
                "src/scene/**",
                "src/main.ts",
                "src/io/AgentImageGen.ts",
                // Deeply Babylon.js-integrated — require full 3D scene mocking
                "src/ui/CardDashboard.ts",
                "src/ui/SocialDashboard.ts",
                "src/ui/AgentHUD.ts",
            ],
            // Ratchet baseline: set to the current measured floor so CI gates
            // regressions today. Raise these toward the goal (95/95/88/95) as
            // coverage improves — never lower them.
            thresholds: {
                lines: 70,
                functions: 65,
                branches: 54,
                statements: 69,
            },
        },
    },
});
