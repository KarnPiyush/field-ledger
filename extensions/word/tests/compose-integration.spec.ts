import { expect, test } from "@playwright/test";

test.skip(
  process.env.FIELDLEDGER_COMPOSE_E2E !== "1",
  "Set FIELDLEDGER_COMPOSE_E2E=1 after the Compose stack is healthy",
);

test("real Compose bootstraps Word, submits a proposal, and hands off to mobile review", async ({ page }) => {
  const wordUrl = process.env.FIELDLEDGER_WORD_URL ?? "http://127.0.0.1:4174";
  const mobileUrl = process.env.FIELDLEDGER_MOBILE_URL ?? "http://127.0.0.1:4173";
  const apiRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/v1/")) apiRequests.push(`${request.method()} ${path}`);
  });
  await page.goto(`${wordUrl}/taskpane.html`);
  await expect(page.getByText("Browser demo")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill(
    "Prepare a cited proposal for Change order 014",
  );
  await page.getByRole("button", { name: "Queue command" }).click();
  const review = page.getByRole("button", { name: "Open proposal review" });
  await expect(review).toBeVisible({ timeout: 30_000 });

  const popupPromise = page.waitForEvent("popup");
  await review.click();
  const popup = await popupPromise;
  const escapedMobileUrl = mobileUrl.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  await expect(popup).toHaveURL(new RegExp(`^${escapedMobileUrl}/review/[^/]+/1$`));
  await popup.close();
  expect(apiRequests).toEqual(expect.arrayContaining([
    "POST /api/v1/auth/demo-session",
    "POST /api/v1/demo/seed",
    expect.stringMatching(/^POST \/api\/v1\/documents\/[^/]+\/edits$/),
  ]));
  expect(apiRequests.some((request) => /^GET \/api\/v1\/ops\/provider-jobs\/[^/]+$/.test(request)))
    .toBe(false);
});
