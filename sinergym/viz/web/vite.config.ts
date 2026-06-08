import { defineConfig } from "vite";

// In dev, proxy /api to the python viz server so geometry + Fuseki work same-origin.
// Live MQTT goes straight to mosquitto:9001 over WS (not proxied).
export default defineConfig({
  server: {
    port: 5180,
    proxy: {
      "/api": { target: "http://localhost:8200", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
