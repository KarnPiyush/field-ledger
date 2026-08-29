import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import { PNG } from "pngjs";
import { mkdir } from "node:fs/promises";
import { fileURLToPath, URL } from "node:url";

const screenshotDirectory = fileURLToPath(new URL("../screenshots/", import.meta.url));

const documentId = "00000000-0000-4000-8000-000000000501";
const workflowRunId = "00000000-0000-4000-8000-000000000401";
const reviewBatchId = "00000000-0000-4000-8000-000000000201";

interface BackendState {
  bootstrapFailures: number;
  starts: number;
  lookups: number;
  idempotencyKeys: string[];
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installBackend(page: Page, bootstrapFailures = 0): Promise<BackendState> {
  const state: BackendState = {
    bootstrapFailures,
    starts: 0,
    lookups: 0,
    idempotencyKeys: [],
  };
  await page.context().route("https://review.fieldledger.test/**", async (route) =>
    route.fulfill({ status: 200, contentType: "text/html", body: "<title>FieldLedger review</title>" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/demo-session") {
      expect(request.postDataJSON()).toEqual({ identity: "reviewer" });
      if (state.bootstrapFailures > 0) {
        state.bootstrapFailures -= 1;
        await json(route, { title: "Bootstrap unavailable" }, 503);
        return;
      }
      await json(route, {
        access_token: "signed-word-session",
        actor_id: "00000000-0000-4000-8000-000000000301",
        global_role: "approver",
        source: "demo",
        token_type: "bearer",
      });
      return;
    }
    if (path === "/api/v1/demo/seed") {
      expect(request.headers()["authorization"]).toBe("Bearer signed-word-session");
      expect(request.headers()["idempotency-key"]).toBe("fieldledger-demo-seed:v1");
      await json(route, {
        document_id: documentId,
        project_id: "00000000-0000-4000-8000-000000000101",
        workflow_run_id: workflowRunId,
        review_batch_id: reviewBatchId,
        status: "ready",
        replayed: false,
      });
      return;
    }
    if (path === `/api/v1/documents/${documentId}/edits` && request.method() === "POST") {
      state.starts += 1;
      state.idempotencyKeys.push(request.headers()["idempotency-key"] ?? "");
      expect(request.postDataJSON()).toMatchObject({ workflow_run_id: workflowRunId });
      await json(route, { id: "provider-job-1", review_batch_id: reviewBatchId, status: "PreviewReady" });
      return;
    }
    if (path === `/api/v1/documents/${documentId}/edits/by-idempotency-key`) {
      state.lookups += 1;
      state.idempotencyKeys.push(request.headers()["idempotency-key"] ?? "");
      await json(route, { id: "provider-job-1", review_batch_id: reviewBatchId, status: "PreviewReady" });
      return;
    }
    throw new Error(`Unhandled Word backend route: ${request.method()} ${path}`);
  });
  return state;
}

async function freshDemo(page: Page): Promise<BackendState> {
  const backend = await installBackend(page);
  await page.goto("/taskpane.html");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByText("Browser demo")).toBeVisible();
  return backend;
}

function expectNonblank(buffer: Buffer): void {
  const image = PNG.sync.read(buffer);
  const colors = new Set<string>();
  for (let offset = 0; offset < image.data.length; offset += 4 * 97) {
    colors.add(
      `${image.data[offset]}:${image.data[offset + 1]}:${image.data[offset + 2]}:${image.data[offset + 3]}`,
    );
  }
  expect(colors.size).toBeGreaterThan(8);
}

test.beforeAll(async () => mkdir(screenshotDirectory, { recursive: true }));

test("330px pane survives reload, reconnects once, and hands off proposal review", async ({ page }) => {
  await page.setViewportSize({ width: 330, height: 720 });
  const backend = await freshDemo(page);

  await page.getByRole("button", { name: "Go offline" }).click();
  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill("Update the concrete quantity table");
  await page.getByRole("button", { name: "Queue command" }).click();
  await expect(page.getByText("Saved in this document")).toBeVisible();
  await expect(page.getByText("Waiting to sync")).toBeVisible();

  await page.reload();
  await expect(page.getByText("Update the concrete quantity table")).toBeVisible();
  await page.getByRole("button", { name: "Go online" }).click();
  const review = page.getByRole("button", { name: "Open proposal review" });
  await expect(review).toBeVisible();

  const popupPromise = page.waitForEvent("popup");
  await review.click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(new RegExp(`/review/${reviewBatchId}/1`));
  await popup.close();
  expect(backend.starts).toBe(1);

  const dimensions = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);

  const screenshot = await page.screenshot({
    path: `${screenshotDirectory}/word-taskpane-330.png`,
    fullPage: true,
  });
  expectNonblank(screenshot);
});

test("moved document pauses the queue with reviewed/current detail and no application", async ({ page }) => {
  await page.setViewportSize({ width: 330, height: 720 });
  const backend = await freshDemo(page);
  await page.getByRole("button", { name: "Go offline" }).click();
  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill("Revise the north annex note");
  await page.getByRole("button", { name: "Queue command" }).click();

  await page.getByRole("button", { name: "Move document" }).click();
  await page.getByRole("button", { name: "Go online" }).click();

  const conflictHeading = page.getByRole("heading", { name: "Needs refresh" });
  await expect(conflictHeading).toBeVisible();
  await expect(conflictHeading.locator("..").getByText("Nothing was applied")).toBeVisible();
  await expect(page.getByText("Reviewed", { exact: true })).toBeVisible();
  await expect(page.getByText("Current", { exact: true })).toBeVisible();
  expect(backend.starts).toBe(0);

  const screenshot = await page.screenshot({
    path: `${screenshotDirectory}/word-taskpane-conflict-330.png`,
    fullPage: true,
  });
  expectNonblank(screenshot);
});

test("lost response reconciles with the original idempotency key and one server application", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  const backend = await freshDemo(page);
  await page.getByRole("button", { name: "Lose next response" }).click();
  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill("Prepare proposal review");
  await page.getByRole("button", { name: "Queue command" }).click();

  await expect(page.getByRole("heading", { name: "Needs reconciliation" })).toBeVisible();
  await page.getByRole("button", { name: "Reconcile" }).click();
  await expect(page.getByRole("button", { name: "Open proposal review" })).toBeVisible();

  expect(backend.starts).toBe(1);
  expect(backend.lookups).toBe(1);
  expect(backend.idempotencyKeys).toHaveLength(2);
  expect(new Set(backend.idempotencyKeys).size).toBe(1);

  await page.keyboard.press("Home");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toBeVisible();
  const screenshot = await page.screenshot({
    path: `${screenshotDirectory}/word-taskpane-desktop.png`,
    fullPage: true,
  });
  expectNonblank(screenshot);
});

test("bootstrap failure clears loading and can be retried", async ({ page }) => {
  await page.setViewportSize({ width: 330, height: 720 });
  await installBackend(page, 1);
  await page.goto("/taskpane.html");

  await expect(page.getByRole("alert")).toContainText("Could not connect Word to FieldLedger");
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect(page.getByText("Browser demo")).toBeVisible();
});
