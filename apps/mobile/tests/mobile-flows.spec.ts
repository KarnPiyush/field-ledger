import { expect, test, type Page } from "@playwright/test";

import { batchId, installMobileBackend, saveCanonicalMixedDecisions } from "./backend";

async function openInbox(page: Page) {
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1, name: "Review inbox" })).toBeVisible();
}

test.describe("phone approval", () => {
  test.use({ viewport: { width: 320, height: 700 } });

  test("opens the cached inbox and one-handed review flow at 320 px", async ({ page }) => {
    await installMobileBackend(page);
    await openInbox(page);
    await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Approved change value" })).toBeVisible();
    await expect(page.getByLabel("Item 1 of 5")).toBeVisible();
    await expect(page.locator("del")).toContainText("USD 100000");
    await expect(page.locator("ins")).toContainText("USD 125000");
    await expect(page.getByRole("img", { name: /change-order-014\.docx, paragraph 2/ })).toBeVisible();
  });

  test("requires a rejection note and survives an offline reload", async ({ context, page }) => {
    await installMobileBackend(page);
    await openInbox(page);
    await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
    await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
    await context.setOffline(true);
    await page.getByRole("button", { name: "Reject" }).click();
    await page.getByRole("button", { name: "Save rejection" }).click();
    await expect(page.getByRole("alert")).toContainText("Add a reason before saving");
    await page.getByLabel("Reason for rejection").fill("The source amount is unsupported.");
    await page.getByRole("button", { name: "Save rejection" }).click();
    await expect(page.getByText("Saved on this phone", { exact: true })).toBeVisible();

    await page.reload();
    await expect(page.getByText("Saved on this phone", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByRole("heading", { name: "All caught up" })).toBeVisible();
    await context.setOffline(false);
  });

  test("deduplicates approve double clicks", async ({ context, page }) => {
    await installMobileBackend(page);
    await openInbox(page);
    await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
    await context.setOffline(true);
    await page.getByRole("button", { name: "Approve" }).dblclick();
    await expect(page.getByText("Saved on this phone", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByRole("heading", { name: "All caught up" })).toBeVisible();
    await context.setOffline(false);
  });

  test("stops later commands behind a real 409 and refreshes from authority without overwrite", async ({ context, page }) => {
    const backend = await installMobileBackend(page, { decisionOutcome: "stale" });
    await openInbox(page);
    await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
    await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
    await context.setOffline(true);
    await saveCanonicalMixedDecisions(page);
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByText("5 decisions · Change order 014")).toBeVisible();
    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));

    await expect(page.getByText("Nothing was applied because Change order 014 changed.")).toBeVisible();
    await expect(page.getByText("sha256:reviewed-source")).toBeVisible();
    await expect(page.getByText("sha256:current-source")).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh for review" })).toBeVisible();
    await expect(page.getByRole("button", { name: /force|overwrite/i })).toHaveCount(0);
    await page.getByRole("button", { name: "Refresh for review" }).click();
    await expect(page).toHaveURL(new RegExp(`/review/${batchId}/1$`));
    expect(backend.decisionRequests).toBe(1);
    await page.getByRole("link", { name: "Queue" }).click();
    await expect(page.getByText("Refreshed", { exact: true })).toBeVisible();
  });

  test("hydrates the applied audit delta after reconnect", async ({ context, page }) => {
    const backend = await installMobileBackend(page);
    await openInbox(page);
    await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
    await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
    await context.setOffline(true);
    await saveCanonicalMixedDecisions(page);
    await page.reload();
    await expect(page.getByText("Batch saved in the ordered queue")).toBeVisible();
    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect.poll(() => backend.applied).toBe(true);
    await page.getByRole("link", { name: "Audit" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Audit trail" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: "review.decisions_applied" })).toBeVisible();
    await expect(page.getByText("demo-reviewer")).toBeVisible();
    await expect(page.getByText("ui", { exact: true })).toBeVisible();
    await expect(page.getByText("not-recorded")).toBeVisible();
    expect(backend.decisionRequests).toBe(1);
    expect(backend.decisionBodies[0]?.decisions).toEqual([
      { item_id: "item-1", decision: "approve" },
      { item_id: "item-2", decision: "reject", note: "Completion evidence needs field verification." },
      { item_id: "item-3", decision: "approve" },
      { item_id: "item-4", decision: "approve" },
      { item_id: "item-5", decision: "approve" },
    ]);
  });
});
