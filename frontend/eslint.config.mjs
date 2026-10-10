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
        // Test suites legitimately have long describe/it blocks with many
        // sequential assertions; size/statement caps don't model them well.
        // `any` is also allowed here: mocks and poking private members
        // (`let cd: any`, class-returns-mock) are idiomatic in tests and not
        // shipped. Correctness rules (complexity, prettier, headers, unused)
        // still apply.
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
        // Type-checked rules are too noisy for the test suite (heavy `any`, mock
        // objects, private-member pokes) and add no shipping-code safety — disable
        // them so type-aware linting covers src/ only.
        files: ["**/__tests__/**", "**/*.{test,spec}.{ts,tsx}"],
        ...tseslint.configs.disableTypeChecked,
    },
    {
        // Files served to the browser as plain script rather than through the
        // bundler: the service worker, and the microphone's audio-thread half.
        // `public` was ignored above and every block below matches only
        // .ts/.tsx, so lifting the ignore alone still left them unchecked. Their
        // globals come from the scopes they run in rather than from the DOM,
        // which is why they need a block of their own.
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
                // The AudioWorkletGlobalScope, where the capture processor runs.
                AudioWorkletProcessor: "readonly",
                registerProcessor: "readonly",
                sampleRate: "readonly",
                currentTime: "readonly",
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
