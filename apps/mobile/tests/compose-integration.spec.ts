import { expect, test } from "@playwright/test";

import { saveCanonicalMixedDecisions } from "./backend";

test.skip(
  process.env.FIELDLEDGER_COMPOSE_E2E !== "1",
  "Set FIELDLEDGER_COMPOSE_E2E=1 after the Compose stack is healthy",
);

test("real Compose applies one offline five-item batch and hydrates one audit event", async ({ context, page }) => {
  const mobileUrl = process.env.FIELDLEDGER_MOBILE_URL ?? "http://127.0.0.1:4173";
  let decisionPosts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/reviews\/[^/]+\/decisions$/.test(new URL(request.url()).pathname)) {
      decisionPosts += 1;
    }
  });
  await page.goto(`${mobileUrl}/audit`);
  await expect(page.getByRole("heading", { name: "Audit trail" })).toBeVisible();
  const appliedHeadings = page.getByRole("heading", {
    level: 2,
    name: "review.decisions_applied",
  });
  const appliedBefore = await appliedHeadings.count();

  await page.goto(`${mobileUrl}/inbox`);
  const canonicalBatch = page.getByRole("link", {
    name: "Riverside Medical Center, 5 items",
  });
  await expect(canonicalBatch).toBeVisible();
  await canonicalBatch.click();
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
  await context.setOffline(true);
  await saveCanonicalMixedDecisions(page);
  await page.reload();
  await expect(page.getByText("Batch saved in the ordered queue")).toBeVisible();

  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.getByRole("link", { name: "Queue" }).click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("5 decisions · Change order 014")).toBeVisible();

  await page.getByRole("link", { name: "Audit" }).click();
  await expect(appliedHeadings).toHaveCount(appliedBefore + 1);
  const applied = page.locator(".audit-row").filter({ hasText: "review.decisions_applied" }).first();
  await expect(applied).toContainText("demo-reviewer");
  await expect(applied).toContainText("ui");
  await expect(applied).toContainText("not-recorded");
  expect(decisionPosts).toBe(1);
});
