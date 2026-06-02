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
      thresholds: {
        lines: 95,
        functions: 95,
        branches: 88,
        statements: 95,
      },
    },
  },
});
