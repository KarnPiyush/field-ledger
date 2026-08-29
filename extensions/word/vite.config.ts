import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig, type ProxyOptions } from "vite";

function apiProxy(): ProxyOptions {
  return {
    target: process.env.FIELDLEDGER_API_TARGET ?? "http://127.0.0.1:8000",
    changeOrigin: true,
    configure(proxy) {
      proxy.on("proxyReq", (request, incoming) => {
        if (!incoming.url?.startsWith("/api/v1/auth/demo-session")) return;
        const secret = process.env.FIELDLEDGER_DEMO_BOOTSTRAP_SECRET;
        if (secret) request.setHeader("X-FieldLedger-Demo-Bootstrap", secret);
      });
    },
  };
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@fieldledger/api-client": fileURLToPath(
        new URL("../../packages/api-client/src/index.ts", import.meta.url),
      ),
      "@fieldledger/offline-queue": fileURLToPath(
        new URL("../../packages/offline-queue/src/index.ts", import.meta.url),
      ),
    },
  },
  build: {
    rollupOptions: {
      input: {
        taskpane: fileURLToPath(new URL("taskpane.html", import.meta.url)),
        review: fileURLToPath(new URL("review.html", import.meta.url)),
      },
    },
  },
  server: { proxy: { "/api": apiProxy() } },
  preview: { proxy: { "/api": apiProxy() } },
});
