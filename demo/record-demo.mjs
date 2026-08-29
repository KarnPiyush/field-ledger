import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const requireFromMobile = createRequire(resolve(root, "apps/mobile/package.json"));
const { chromium, expect } = requireFromMobile("@playwright/test");

const mobile = process.env.FIELDLEDGER_MOBILE_URL ?? "http://127.0.0.1:4173";
const word = process.env.FIELDLEDGER_WORD_URL ?? "http://127.0.0.1:4174";
const videoDirectory = resolve(here, ".recording");
await mkdir(videoDirectory, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1600, height: 900 },
  recordVideo: { dir: videoDirectory, size: { width: 1600, height: 900 } },
});
const page = await context.newPage();
let decisionPostCount = 0;
let wordStartCount = 0;
let wordLookupCount = 0;
const wordStartKeys = [];
const wordLookupKeys = [];

context.on("request", (request) => {
  const path = new URL(request.url()).pathname;
  const idempotencyKey = request.headers()["idempotency-key"];
  if (request.method() === "POST" && /^\/api\/v1\/reviews\/[^/]+\/decisions$/.test(path)) {
    decisionPostCount += 1;
  }
  if (request.method() === "POST" && /^\/api\/v1\/documents\/[^/]+\/edits$/.test(path)) {
    wordStartCount += 1;
    wordStartKeys.push(idempotencyKey);
  }
  if (request.method() === "GET" && /\/edits\/by-idempotency-key$/.test(path)) {
    wordLookupCount += 1;
    wordLookupKeys.push(idempotencyKey);
  }
});

async function caption(chapter, text, waitSeconds) {
  await page.evaluate(
    ({ chapter, text }) => {
      document.querySelector("#fieldledger-demo-caption")?.remove();
      const panel = document.createElement("aside");
      panel.id = "fieldledger-demo-caption";
      panel.setAttribute("aria-label", "Demo narration caption");
      panel.style.cssText = [
        "position:fixed",
        "right:24px",
        "bottom:24px",
        "z-index:2147483647",
        "width:min(520px,calc(100vw - 48px))",
        "box-sizing:border-box",
        "pointer-events:none",
        "padding:16px 18px",
        "border:1px solid #f2c94c",
        "border-radius:6px",
        "background:rgba(18,25,21,.94)",
        "color:#fff",
        "font:500 20px/1.35 Arial,sans-serif",
        "box-shadow:0 8px 28px rgba(0,0,0,.3)",
      ].join(";");
      const label = document.createElement("strong");
      label.textContent = chapter;
      label.style.cssText = "display:block;color:#f2c94c;font-size:14px;margin-bottom:6px;text-transform:uppercase";
      const body = document.createElement("span");
      body.textContent = text;
      panel.append(label, body);
      document.body.append(panel);
    },
    { chapter, text },
  );
  await page.waitForTimeout(waitSeconds * 1000);
}

async function saveCanonicalMixedDecisions() {
  const decisions = ["approve", "reject", "approve", "approve", "approve"];
  for (let index = 0; index < decisions.length; index += 1) {
    if (decisions[index] === "reject") {
      await page.getByRole("button", { name: "Reject" }).click();
      await page.getByLabel("Reason for rejection").fill("Completion evidence needs field verification.");
      await page.getByRole("button", { name: "Save rejection" }).click();
    } else {
      await page.getByRole("button", { name: "Approve" }).click();
    }
    await expect(page.getByText(/Saved on this phone|Batch saved in the ordered queue/)).toBeVisible();
    if (index + 1 < decisions.length) {
      await page.getByRole("button", { name: "Next" }).click();
      await expect(page.getByLabel(`Item ${index + 2} of 5`)).toBeVisible();
    }
  }
}

let recordedPath;
try {
  await page.goto(`${mobile}/inbox`);
  await expect(page.getByRole("heading", { name: "Review inbox" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Riverside Medical Center/ })).toBeVisible();
  await caption(
    "The document pile",
    "The real API seeds a contract, signed change order, site report, invoice, and rules as one five-item cited review batch.",
    13,
  );

  await page.getByRole("link", { name: "Audit" }).click();
  await expect(page.getByRole("heading", { name: "Audit trail" })).toBeVisible();
  const appliedAudits = page.getByRole("heading", {
    level: 2, name: "review.decisions_applied",
  });
  const auditCountBefore = await appliedAudits.count();
  expect(auditCountBefore, "record from freshly reset synthetic state").toBe(0);

  await page.getByRole("link", { name: "Operations" }).click();
  await expect(page.getByRole("heading", { name: "Jobs and recovery" })).toBeVisible();
  await caption(
    "Visible stages and outcomes",
    "Processing, review-ready, completed, failed, cancelled, and reconciliation remain explicit recoverable states.",
    13,
  );

  await page.getByRole("link", { name: "Inbox" }).click();
  await page.getByRole("link", { name: /Riverside Medical Center/ }).click();
  await expect(page.getByLabel("Item 1 of 5")).toBeVisible();
  const firstReviewUrl = page.url();
  let feederIndex;
  for (let index = 1; index <= 5; index += 1) {
    await page.goto(firstReviewUrl.replace(/\/1$/, `/${index}`));
    await expect(page.getByLabel(`Item ${index} of 5`)).toBeVisible();
    if (await page.getByRole("heading", { name: "Feeder allowance" }).count() === 1) {
      feederIndex = index;
      break;
    }
  }
  expect(feederIndex, "the canonical batch must contain Feeder allowance").toBeTruthy();
  await expect(page.getByRole("heading", { name: "Feeder allowance" })).toBeVisible();
  await expect(page.getByRole("img", { name: /change-order-014\.docx, paragraph 2/i })).toBeVisible();
  await caption(
    "Exact citation evidence",
    "The feeder allowance changes from USD 42,500 to USD 47,250 and cites the signed change-order paragraph. The conflicting site-report value stays visible.",
    17,
  );
  await page.goto(firstReviewUrl);
  await expect(page.getByLabel("Item 1 of 5")).toBeVisible();

  await expect.poll(() => page.evaluate(() => navigator.serviceWorker?.controller !== null)).toBe(true);
  await context.setOffline(true);
  await saveCanonicalMixedDecisions();
  await expect(page.getByText("Batch saved in the ordered queue", { exact: true })).toBeVisible();
  expect(decisionPostCount, "offline decisions must not reach the API").toBe(0);
  await caption(
    "Offline mixed decisions",
    "All five choices are saved locally. Item two is rejected with a note; the other four are approved. Saved never means applied.",
    17,
  );

  await page.reload();
  await expect(page.getByText("Batch saved in the ordered queue", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Queue" }).click();
  await expect(page.getByText("5 decisions · Change order 014", { exact: true })).toBeVisible();
  await caption(
    "Offline reload and ordered batch",
    "IndexedDB restores one ordered five-decision command with its original idempotency identity after a full reload.",
    14,
  );

  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.getByText("Applied", { exact: true })).toBeVisible({ timeout: 30_000 });
  expect(decisionPostCount, "one reviewed batch must produce one decision request").toBe(1);
  await caption(
    "Reconnect and apply once",
    "Reconnect sends one batch request containing all five reviewed items. The queue marks the authoritative result Applied.",
    20,
  );

  await page.getByRole("link", { name: "Audit" }).click();
  await expect(page.getByRole("heading", { name: "Audit trail" })).toBeVisible();
  await expect(page.getByRole("heading", {
    level: 2, name: "review.decisions_applied",
  })).toBeVisible();
  const appliedAudit = page.locator(".audit-row")
    .filter({ hasText: "review.decisions_applied" })
    .first();
  await expect(appliedAudit).toContainText("demo-reviewer");
  await expect(appliedAudit).toContainText("ui");
  await expect(appliedAudit.getByText("not-recorded", { exact: true })).toBeVisible();
  const auditCountAfter = await page.getByRole("heading", {
    level: 2, name: "review.decisions_applied",
  }).count();
  expect(auditCountAfter).toBe(auditCountBefore + 1);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await page.waitForTimeout(1_000);
  expect(decisionPostCount, "a repeated reconnect must not resubmit an applied command").toBe(1);
  expect(await page.getByRole("heading", {
    level: 2, name: "review.decisions_applied",
  }).count()).toBe(auditCountAfter);
  await caption(
    "Authoritative audit delta",
    "The backend adds exactly one review.decisions_applied event for demo-reviewer. Repeating reconnect adds no request and no second audit event.",
    19,
  );

  await page.goto(`${word}/taskpane.html`);
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByText("Browser demo")).toBeVisible();
  await page.getByRole("button", { name: "Go offline" }).click();
  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill(
    "Update the concrete quantity table and prepare a proposal for human review.",
  );
  await page.getByRole("button", { name: "Queue command" }).click();
  await expect(page.getByText("Saved in this document")).toBeVisible();
  await expect(page.getByText("Waiting to sync")).toBeVisible();
  await page.reload();
  await expect(page.getByText(/Update the concrete quantity table/)).toBeVisible();
  await page.getByRole("button", { name: "Move document" }).click();
  await page.getByRole("button", { name: "Go online" }).click();
  await expect(page.getByRole("heading", { name: "Needs refresh" })).toBeVisible();
  await expect(page.getByText("Nothing was applied", { exact: true })).toBeVisible();
  await caption(
    "Word offline queue and moved-document stop",
    "The Word instruction survives reopening, but a changed document fingerprint stops before any Word content or provider request changes.",
    27,
  );

  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByText("Browser demo")).toBeVisible();
  const startsBeforeLostResponse = wordStartCount;
  const lookupsBeforeLostResponse = wordLookupCount;
  await page.getByRole("button", { name: "Lose next response" }).click();
  await page.getByRole("textbox", { name: "Instruction", exact: true }).fill(
    "Prepare the cited proposal review and export record.",
  );
  await page.getByRole("button", { name: "Queue command" }).click();
  await expect(page.getByRole("heading", { name: "Needs reconciliation" })).toBeVisible();
  await page.getByRole("button", { name: "Reconcile" }).click();
  const openReview = page.getByRole("button", { name: "Open proposal review" });
  await expect(openReview).toBeVisible();
  expect(wordStartCount - startsBeforeLostResponse).toBe(1);
  expect(wordLookupCount - lookupsBeforeLostResponse).toBe(1);
  expect(wordStartKeys.at(-1)).toBeTruthy();
  expect(wordLookupKeys.at(-1)).toBe(wordStartKeys.at(-1));
  const popupPromise = page.waitForEvent("popup");
  await openReview.click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/review\/[^/]+\/1$/);
  await popup.close();
  await caption(
    "Lost response and proposal review",
    "One start and one non-mutating lookup reuse the same idempotency key before the proposal review handoff becomes available.",
    20,
  );

  await caption(
    "Honest local-only boundary",
    "The demo uses synthetic data and the deterministic no-key adapter. Native Word sideload, live SuperDocs, OCR, deployment, and upload are not claimed.",
    18,
  );
} finally {
  const video = page.video();
  await context.close();
  recordedPath = await video.path();
  await browser.close();
}

process.stdout.write(recordedPath);
