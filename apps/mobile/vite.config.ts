import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig, type ProxyOptions } from "vite";
import { VitePWA } from "vite-plugin-pwa";

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
  resolve: {
    alias: {
      "@fieldledger/api-client": fileURLToPath(new URL("../../packages/api-client/src/index.ts", import.meta.url)),
      "@fieldledger/offline-queue": fileURLToPath(new URL("../../packages/offline-queue/src/index.ts", import.meta.url)),
    },
  },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      manifest: {
        name: "FieldLedger",
        short_name: "FieldLedger",
        description: "Offline construction review and approval queue",
        theme_color: "#17211b",
        background_color: "#f3f5f1",
        display: "standalone",
        start_url: "/inbox",
        scope: "/",
        icons: [
          { src: "/icons/fieldledger.svg", sizes: "any", type: "image/svg+xml", purpose: "any" },
          { src: "/icons/fieldledger-maskable.svg", sizes: "any", type: "image/svg+xml", purpose: "maskable" },
        ],
      },
      injectManifest: { globPatterns: ["**/*.{js,css,html,svg,png,woff2}"] },
      devOptions: { enabled: true, type: "module" },
    }),
  ],
  server: { port: 4176, strictPort: false, proxy: { "/api": apiProxy() } },
  preview: { port: 4176, strictPort: false, proxy: { "/api": apiProxy() } },
});
