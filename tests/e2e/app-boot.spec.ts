import { expect, test } from "@playwright/test";
import { openApp, startNewSession } from "./helpers/app";
import { currentSessionId, readSession, resetE2E } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("app boots and creates a local session", async ({ page, request }) => {
  await openApp(page);
  await expect(page.getByTestId("skill-binding-status")).toContainText("Skill: NEW");

  await startNewSession(page);
  const sessionId = await currentSessionId(page);
  const session = await readSession(request, sessionId);

  expect(session.id).toBe(sessionId);
  expect(session.current_stage).toBe("prepare");
  await expect(page.getByTestId("message-input")).toBeEnabled();
});

for (const status of [502, 503]) {
  test(`auth status ${status} reports an error without requesting sign-in`, async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Microsoft token renewal is unavailable. Please retry." }),
    }));
    await page.goto("/");
    await expect(page.locator("#authStatus")).toHaveText("Sign-in check failed; reload to retry");
    await expect(page.locator("#loginGate")).toBeHidden();

    await page.unroute("**/api/auth/status");
    await page.reload();
    await expect(page.locator("#authStatus")).toHaveText("Microsoft signed in");
    await expect(page.locator("#loginGate")).toBeHidden();
  });
}

test("an explicitly unauthenticated status still requests sign-in", async ({ page }) => {
  await page.route("**/api/auth/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ authenticated: false }),
  }));
  await page.goto("/");
  await expect(page.locator("#loginGate")).toBeVisible();
});

test("a protected API returning 401 still requests sign-in", async ({ page }) => {
  await openApp(page);
  await expect(page.locator("#authStatus")).toHaveText("Microsoft signed in");
  await page.route("**/api/neighbor-notices/summary", (route) => route.fulfill({
    status: 401,
    contentType: "application/json",
    body: JSON.stringify({ detail: "Sign in required." }),
  }));
  await page.evaluate(() => window.fetch("/api/neighbor-notices/summary"));
  await expect(page.locator("#loginGate")).toBeVisible();
});

test("resizes the main workspace panes from the center splitter", async ({ page }) => {
  await openApp(page);
  const resizer = page.getByTestId("workspace-resizer");
  await expect(resizer).toBeVisible();

  const before = await page.locator(".workspace").evaluate((node) => getComputedStyle(node).gridTemplateColumns);
  const box = await resizer.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 120, box!.y + box!.height / 2);
  await page.mouse.up();

  const after = await page.locator(".workspace").evaluate((node) => getComputedStyle(node).gridTemplateColumns);
  expect(after).not.toBe(before);
});

test("resizes the skill source and preview panes from the files splitter", async ({ page }) => {
  await openApp(page);
  await page.getByTestId("tab-files").click();
  const resizer = page.getByTestId("files-resizer");
  await expect(resizer).toBeVisible();

  const before = await page.locator(".skill-md-workspace").evaluate((node) => getComputedStyle(node).gridTemplateColumns);
  const box = await resizer.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + 100, box!.y + box!.height / 2);
  await page.mouse.up();

  const after = await page.locator(".skill-md-workspace").evaluate((node) => getComputedStyle(node).gridTemplateColumns);
  expect(after).not.toBe(before);
});
