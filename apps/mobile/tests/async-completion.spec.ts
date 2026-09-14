import { expect, test } from "@playwright/test";

import { installMobileBackend, saveCanonicalMixedDecisions } from "./backend";

test.use({ viewport: { width: 1280, height: 800 }, serviceWorkers: "block" });

test("bootstrap error clears loading and retry restores the authoritative inbox", async ({ page }) => {
  await installMobileBackend(page, { bootstrapFailures: 1 });
  await page.goto("/inbox");

  await expect(page.getByRole("alert")).toContainText("Could not load FieldLedger data");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Review inbox" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Riverside Medical Center/ })).toBeVisible();
  await expect(page.getByText("Loading saved field work")).toHaveCount(0);
});

test("PreviewReady completes the drain and restores the sync control", async ({ page }) => {
  const backend = await installMobileBackend(page, { decisionOutcome: "processing" });
  await page.goto("/inbox");
  await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
  await saveCanonicalMixedDecisions(page);
  await page.getByRole("button", { name: "Back to inbox" }).click();
  const sync = page.getByRole("button", { name: "Sync review batches" });
  await sync.click();

  await expect.poll(() => backend.applied).toBe(true);
  await expect(sync).toBeEnabled();
  await expect(page.getByText(/^Syncing /)).toHaveCount(0);
});

test("lost response survives reload and replays the original token, body and key", async ({ page }) => {
  const backend = await installMobileBackend(page, { decisionOutcome: "lost" });
  await page.goto("/inbox");
  await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
  await saveCanonicalMixedDecisions(page);
  await page.getByRole("button", { name: "Back to inbox" }).click();
  await page.getByRole("button", { name: "Sync review batches" }).click();
  await page.getByRole("link", { name: "Queue" }).click();

  await expect(page.getByText("Reconcile", { exact: true })).toBeVisible();
  await page.reload();
  const reconcile = page.getByRole("button", { name: "Reconcile outcome" });
  await expect(reconcile).toBeEnabled();
  backend.decisionOutcome = "completed";
  await reconcile.click();
  await expect(page.getByText("Applied", { exact: true })).toBeVisible();
  expect(backend.decisionRequests).toBe(2);
  expect(new Set(backend.idempotencyKeys).size).toBe(1);
  expect(backend.confirmationRequests).toBe(1);
  expect(backend.decisionBodies[1]).toEqual(backend.decisionBodies[0]);
});

test("corrupt queue stays visible for export and explicit reset", async ({ page }) => {
  await installMobileBackend(page);
  await page.goto("/inbox");
  await expect(page.getByRole("link", { name: /Riverside Medical Center/ })).toBeVisible();
  await page.evaluate(async () => {
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open("fieldledger-mobile");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction("offlineCommands", "readwrite");
        transaction.onerror = () => reject(transaction.error);
        transaction.oncomplete = () => resolve();
        transaction.objectStore("offlineCommands").put({
          schemaVersion: 1,
          id: "corrupt-command",
          clientId: "mobile:demo-reviewer",
          projectId: "00000000-0000-4000-8000-000000000101",
          sequence: 1,
          idempotencyKey: "mobile:demo-reviewer:1",
          deduplicationKey: "corrupt",
          reviewedDigest: "sha256:reviewed",
          payloadHash: "sha256:corrupt",
          payload: {},
          state: "pending",
          createdAt: "2026-08-24T01:00:00Z",
          attempts: 0,
        });
      };
    });
  });
  await page.reload();

  await expect(page.getByRole("heading", { name: "Saved queue cannot be read" })).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export queue" }).click();
  await expect((await downloadPromise).suggestedFilename()).toBe("fieldledger-mobile-queue.json");
  await page.getByRole("button", { name: "Reset queue" }).click();
  await expect(page.getByRole("heading", { name: "Saved queue cannot be read" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /Riverside Medical Center/ })).toBeVisible();
});
