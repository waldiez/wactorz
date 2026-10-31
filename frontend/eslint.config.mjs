/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import headers from "eslint-plugin-headers";
import eslintPluginPrettierRecommended from "eslint-plugin-prettier/recommended";
import importPlugin from "eslint-plugin-import";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

const owner = "Waldiez";
const startYear = 2025;
const spdxIdentifier = "Apache-2.0";
const currentYear = new Date().getFullYear();
const ownerAndContributors = `${owner} & contributors`;

const project = "./tsconfig.json";

// noinspection JSCheckFunctionSignatures
const defaultConfig = defineConfig({
    files: ["**/*.{ts,tsx}"],
    extends: [
        ...tseslint.configs.recommendedTypeChecked,
        eslintPluginPrettierRecommended,
        importPlugin.flatConfigs.typescript,
    ],
    languageOptions: {
        parserOptions: {
            projectService: {
                allowDefaultProject: ["vite.config.ts", "vitest.config.ts"],
            },
            tsconfigRootDir: import.meta.dirname,
        },
    },
    settings: {
        "import/resolver": {
            typescript: {
                alwaysTryTypes: true,
                project,
            },
            node: true,
        },
    },
    plugins: {
        headers,
    },
    rules: {
        // Formatting options live in .prettierrc.json so the standalone
        // `prettier` CLI (fmt / prettier:check) and eslint-plugin-prettier
        // share one source of truth. Do NOT inline options here.
        "prettier/prettier": "error",
        "no-unused-vars": "off",
        "@typescript-eslint/no-unused-vars": [
            "error",
            {
                args: "all",
                argsIgnorePattern: "^_",
                varsIgnorePattern: "^_",
                caughtErrorsIgnorePattern: "^_",
            },
        ],
        "@typescript-eslint/no-explicit-any": "error",
        // Guard the init-time circular-import invariant statically (the codebase
        // leans on function-local imports to avoid cycles at module load).
        "import/no-cycle": "error",
        // Every request goes through src/io/api.ts, which gives it a timeout and
        // the server's words for a refusal.
        "no-restricted-globals": [
            "error",
            { name: "fetch", message: "Ask through src/io/api.ts (requestJson, requestBytes)." },
        ],
        "no-restricted-properties": [
            "error",
            {
                object: "window",
                property: "fetch",
                message: "Ask through src/io/api.ts (requestJson, requestBytes).",
            },
            {
                object: "globalThis",
                property: "fetch",
                message: "Ask through src/io/api.ts (requestJson, requestBytes).",
            },
        ],
        // The models never reach into the views: a view observes a model, and
        // a model that imported one could be tested only with a DOM. Nor does
        // transport or config import a view, or core an extension.
        "import/no-restricted-paths": [
            "error",
            {
                zones: [
                    {
                        target: ["./src/agents", "./src/chat", "./src/models", "./src/types"],
                        from: "./src/ui",
                        message: "Models do not import views; let the view observe the model.",
                    },
                    {
                        target: ["./src/io", "./src/config", "./src/events.ts"],
                        from: "./src/ui",
                        message: "Transport does not import views: emit an event, or take a callback.",
                    },
                    {
                        target: ["./src/io", "./src/config", "./src/events.ts", "./src/agents", "./src/ui"],
                        from: "./src/ext",
                        message:
                            "Core does not import extensions: they register themselves (see CONTRIBUTING).",
                    },
                ],
            },
        ],
        "@typescript-eslint/no-namespace": "off",
        "@typescript-eslint/no-unused-expressions": "off",
        "@typescript-eslint/no-use-before-define": "off",
        curly: ["error", "all"],
        // Enforce strict equality, but allow the `x == null` / `x != null`
        // idiom (matches both null and undefined) — rewriting those to `===`
        // would silently change behavior.
        eqeqeq: ["error", "always", { null: "ignore" }],
        "prefer-arrow-callback": "error",
        complexity: ["error", 15],
        "max-depth": ["error", 4],
        "max-nested-callbacks": ["error", 4],
        "max-statements": ["error", 20, { ignoreTopLevelFunctions: true }],
        "max-lines": ["error", { max: 500, skipBlankLines: true, skipComments: true }],
        "max-lines-per-function": ["error", { max: 80, skipBlankLines: true, skipComments: true }],
        "headers/header-format": [
            "error",
            {
                source: "string",
                content: "{licenseLine}\n{copyrightLine}",
                variables: {
                    licenseLine: `SPDX-License-Identifier: ${spdxIdentifier}`,
                    copyrightLine: `Copyright ${startYear} - ${currentYear} ${ownerAndContributors}`,
                },
            },
        ],
    },
});

export default [
    {
        ignores: ["node_modules", "dist", "coverage", "**/assets/**"],
    },
    ...defaultConfig.map(config => ({
        ...config,
        files: ["**/*.{ts,tsx}"],
    })),
    {
        // The client itself, and tests, which stub what it calls.
        files: ["src/io/api.ts", "**/__tests__/**", "**/*.{test,spec}.{ts,tsx}"],
        rules: {
            "no-restricted-globals": "off",
            "no-restricted-properties": "off",
        },
    },
    {
        // Test suites legitimately have long describe/it blocks with many
        // sequential assertions; size/statement caps don't model them well.
        // `any` is allowed for the mocks of whole modules (a class whose
        // constructor returns the mock) and the browser APIs happy-dom lacks.
        // A test reaches what it checks through public seams -- the store, the
        // bus, the screen -- and never a private member. Correctness rules
        // (complexity, prettier, headers, unused) still apply.
        files: ["**/__tests__/**", "**/*.{test,spec}.{ts,tsx}"],
        rules: {
            "max-lines": "off",
            "max-lines-per-function": "off",
            "max-statements": "off",
            "max-nested-callbacks": "off",
            "@typescript-eslint/no-explicit-any": "off",
        },
    },
    {
        // Suites that test through public seams only: an `any` here would be a
        // way back to the private members they no longer reach.
        files: [
            "src/__tests__/dashboard.ts",
            "src/__tests__/agentstore.test.ts",
            "src/__tests__/carddashboard-*.test.ts",
            "src/__tests__/axe.test.ts",
        ],
        rules: {
            "@typescript-eslint/no-explicit-any": "error",
        },
    },
    {
        // Type-checked rules are too noisy for the test suite (heavy `any`, mock
        // objects) and add no shipping-code safety — disable
        // them so type-aware linting covers src/ only.
        files: ["**/__tests__/**", "**/*.{test,spec}.{ts,tsx}"],
        ...tseslint.configs.disableTypeChecked,
    },
    {
        // The service worker ships to users but was linted by nothing: `public`
        // was ignored above, and every block below matches only .ts/.tsx — so
        // lifting the ignore alone still left it unchecked. It is plain script
        // (no bundler, no imports), and its globals come from the ServiceWorker
        // scope rather than the DOM, so it needs its own block.
        files: ["public/**/*.js"],
        languageOptions: {
            ecmaVersion: "latest",
            sourceType: "script",
            globals: {
                self: "readonly",
                caches: "readonly",
                clients: "readonly",
                fetch: "readonly",
                console: "readonly",
                Response: "readonly",
                Request: "readonly",
                URL: "readonly",
            },
        },
        plugins: eslintPluginPrettierRecommended.plugins,
        rules: {
            ...eslintPluginPrettierRecommended.rules,
            "no-undef": "error",
            "no-unused-vars": "error",
        },
    },
];
