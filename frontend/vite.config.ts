import { defineConfig } from "vite";

export default defineConfig({
  // Read .env from the repo root (one level up from frontend/)
  envDir: "..",
  server: {
    port: 3000,
    // Don't auto-open a browser tab — Frontend opens its own WebView window.
    // Set VITE_OPEN=true to restore the plain-browser dev experience.
    open: process.env["VITE_OPEN"] === "true",
    proxy: {
      // Forward to the embedded Rust backend (same port as __WACTORZ_API_PORT).
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
          // Babylon.js core + GUI + loaders → dedicated chunk (large but cacheable)
          if (id.includes("@babylonjs/core")) return "babylon-core";
          if (id.includes("@babylonjs/gui")) return "babylon-gui";
          if (id.includes("@babylonjs/loaders")) return "babylon-loaders";
          // mqtt.js + ws deps
          if (id.includes("mqtt") || id.includes("node_modules")) return "vendor";
        },
      },
    },
    // Babylon.js is a 3D engine; its chunk is legitimately large (≈1.1 MB gz)
    chunkSizeWarningLimit: 6000,
  },
  optimizeDeps: {
    // Babylon.js uses dynamic imports internally; exclude from pre-bundling
    exclude: ["@babylonjs/core", "@babylonjs/gui", "@babylonjs/loaders"],
  },
});
