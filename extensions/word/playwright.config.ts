import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "test-results",
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: "http://127.0.0.1:4177",
    browserName: "chromium",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "pnpm build && pnpm preview --port 4177",
    env: {
      VITE_FIELDLEDGER_API_URL: "/",
      VITE_FIELDLEDGER_REVIEW_URL: "https://review.fieldledger.test",
    },
    url: "http://127.0.0.1:4177/taskpane.html",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
