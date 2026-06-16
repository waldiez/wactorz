import { defineConfig } from "vite";

export default defineConfig({
  // Read .env from the repo root (one level up from frontend/)
  envDir: "..",
  server: {
    port: 3000,
    // Don't auto-open a browser tab by default.
    // Set VITE_OPEN=true to restore the plain-browser dev experience.
    open: process.env["VITE_OPEN"] === "true",
    proxy: {
      // Forward API and WebSocket calls to the local monitor server in dev.
      "/api": { target: "http://localhost:8888", changeOrigin: true },
      "/ws":   { target: "ws://localhost:8888",  ws: true },
      "/mqtt": { target: "ws://localhost:8888",  ws: true },
    },
  },
  base: "./",
  build: {
    outDir: "../static/app",
    emptyOutDir: true,
    sourcemap: true,
    target: "es2022",
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          // mqtt.js + ws deps
          if (id.includes("mqtt") || id.includes("node_modules")) return "vendor";
        },
      },
    },
  },
});
