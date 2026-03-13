import { defineConfig } from "vite";

export default defineConfig({
  // Read .env from the repo root (one level up from frontend/)
  envDir: "..",
  server: {
    port: 3000,
    open: true,
    proxy: {
      // Proxy REST API calls to the Rust server
      "/api": {
        target: "http://localhost:8080",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // Proxy WebSocket upgrade for the AgentFlow WS bridge
      "/ws": {
        target: "ws://localhost:8081",
        ws: true,
      },
      // Proxy MQTT WebSocket (matches the nginx /mqtt path used in production)
      "/mqtt": {
        target: "ws://localhost:9001",
        ws: true,
      },
    },
  },
  base: "./",
  build: {
    outDir: "../static/app",
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
