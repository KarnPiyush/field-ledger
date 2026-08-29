import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PNG } from "pngjs";

import { batchId, installMobileBackend } from "./backend";

const screens = [
  { name: "inbox-320", path: "/inbox", width: 320, height: 700 },
  { name: "review-320", path: `/review/${batchId}/1`, width: 320, height: 700 },
  { name: "stale-320", path: "/queue", width: 320, height: 700 },
  { name: "queue-390", path: "/queue", width: 390, height: 844 },
  { name: "inbox-390", path: "/inbox", width: 390, height: 844 },
  { name: "review-390", path: `/review/${batchId}/1`, width: 390, height: 844 },
  { name: "stale-390", path: "/queue", width: 390, height: 844 },
  { name: "inbox-768", path: "/inbox", width: 768, height: 1024 },
  { name: "review-768", path: `/review/${batchId}/1`, width: 768, height: 1024 },
  { name: "stale-768", path: "/queue", width: 768, height: 1024 },
  { name: "queue-768", path: "/queue", width: 768, height: 1024 },
  { name: "ops-1280", path: "/ops", width: 1280, height: 800 },
];

for (const screen of screens) {
  test(`${screen.name} is nonblank, overflow-free, and captured`, async ({ page }) => {
    await installMobileBackend(page);
    await page.setViewportSize({ width: screen.width, height: screen.height });
    await page.goto(screen.path);
    await expect(page.locator("main h1")).toBeVisible();
    if (screen.path.startsWith("/review")) await page.locator(".diff-block").scrollIntoViewIfNeeded();
    const dimensions = await page.evaluate(() => ({
      bodyWidth: document.body.scrollWidth,
      viewportWidth: document.documentElement.clientWidth,
      textLength: document.body.innerText.trim().length,
      overlap: [...document.querySelectorAll<HTMLElement>("[data-no-overlap]")].some((element) => {
        const rect = element.getBoundingClientRect();
        return rect.right > window.innerWidth || rect.left < 0;
      }),
      undersizedTargets: [...document.querySelectorAll<HTMLElement>("button, .nav-link")].filter((element) => {
        const rect = element.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        return rect.width < 44 || rect.height < 44;
      }).length,
      fixedControlGap: (() => {
        const decision = document.querySelector<HTMLElement>(".decision-bar");
        const nav = document.querySelector<HTMLElement>(".primary-nav");
        if (!decision || !nav || window.innerWidth >= 900) return 0;
        return Math.abs(decision.getBoundingClientRect().bottom - nav.getBoundingClientRect().top);
      })(),
    }));
    expect(dimensions.bodyWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
    expect(dimensions.textLength).toBeGreaterThan(120);
    expect(dimensions.overlap).toBe(false);
    expect(dimensions.undersizedTargets).toBe(0);
    expect(dimensions.fixedControlGap).toBeLessThanOrEqual(1);
    const screenshot = await page.screenshot({ path: `artifacts/screenshots/${screen.name}.png`, fullPage: false });
    const png = PNG.sync.read(screenshot);
    const colors = new Set<string>();
    const stride = Math.max(4, Math.floor((png.width * png.height) / 5_000) * 4);
    for (let offset = 0; offset < png.data.length; offset += stride) {
      colors.add(`${png.data[offset]}:${png.data[offset + 1]}:${png.data[offset + 2]}`);
    }
    expect(colors.size).toBeGreaterThan(20);
  });
}

test("meets WCAG AA automated checks and keyboard focus requirements", async ({ page }) => {
  await installMobileBackend(page);
  await page.setViewportSize({ width: 320, height: 700 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/inbox");
  expect(await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze()).toMatchObject({ violations: [] });
  await page.keyboard.press("Tab");
  const firstFocus = await page.locator(":focus").evaluate((element) => ({
    name: element.getAttribute("aria-label") ?? element.textContent,
    outline: getComputedStyle(element).outlineStyle,
  }));
  expect(firstFocus.name).toBeTruthy();
  expect(firstFocus.outline).not.toBe("none");
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await expect(page.locator('[aria-live="polite"]')).toBeAttached();
});

test("registers an installable PWA shell and reloads it offline", async ({ context, page }) => {
  await installMobileBackend(page);
  await page.goto("/inbox");
  const manifest = await page.locator('link[rel="manifest"]').getAttribute("href");
  expect(manifest).toBeTruthy();
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker?.controller !== null)).toBe(true);
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Review inbox" })).toBeVisible();
  await context.setOffline(false);
});
