/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { defineConfig } from "vite";

export default defineConfig({
    // envDir is deliberately left at its default (this directory) rather than
    // pointed at the repo root. The root `.env` is the backend's secrets file,
    // and Vite inlines every `VITE_`-prefixed value it finds into the client
    // bundle — so anything named that way there would be published, both in the
    // committed `static/app` and in the wheel. Keeping it here also makes the
    // build reproducible: `static/app` is committed, so its contents must not
    // depend on who built it. There are no env files here, by design; runtime
    // configuration reaches the dashboard through `/api/config` instead.
    server: {
        port: 3000,
        // Don't auto-open a browser tab by default.
        // Set VITE_OPEN=true to restore the plain-browser dev experience.
        open: process.env["VITE_OPEN"] === "true",
        proxy: {
            // Forward API and WebSocket calls to the local monitor server in dev.
            "/api": { target: "http://localhost:8888", changeOrigin: true },
            "/ws": { target: "ws://localhost:8888", ws: true },
        },
    },
    base: "./",
    build: {
        outDir: "../static/app",
        emptyOutDir: true,
        sourcemap: false,
        target: "es2022",
        rollupOptions: {
            output: {
                manualChunks: id => {
                    if (id.includes("node_modules")) {
                        return "vendor";
                    }
                },
            },
        },
    },
});
