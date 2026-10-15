import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { openApp } from "./helpers/app";
import { seedSkill } from "./helpers/e2eApi";

test("capture Manage Skills user guide", async ({ page, request }) => {
  test.skip(process.env.SGV2_GUIDE_SCREENSHOTS !== "1", "Opt-in documentation screenshots");
  test.setTimeout(90_000);
  const output = path.resolve("docs/images/manage-skills");
  await mkdir(output, { recursive: true });
  await page.setViewportSize({ width: 1280, height: 1400 });
  expect((await request.post("/api/e2e/reset")).ok()).toBeTruthy();
  for (const name of ["leave-request", "training-demo"]) {
    expect((await seedSkill(request, name)).ok()).toBeTruthy();
  }
  expect((await seedSkill(request, "leave-workflow", "scenario", {
    skill_md: "---\nname: leave-workflow\ndescription: Coordinate employee leave requests.\nmetadata:\n  children: [leave-request]\n---\n# Leave workflow\n",
  })).ok()).toBeTruthy();

  await page.route("**/api/permissions/principals?*", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const principal = params.get("principal_type") === "X"
      ? { name: "HR Skill Users", type: "X" }
      : { name: "alice@example.com", type: "E" };
    await route.fulfill({ json: { principals: principal.name.toLowerCase().includes((params.get("q") || "").toLowerCase()) ? [principal] : [] } });
  });
  await page.route("**/api/skills/training-demo/grants", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const payload = route.request().postDataJSON();
    const response = await request.post("/api/skills/training-demo/grants", { data: { user_upn: payload.user_upn } });
    await route.fulfill({ response });
  });
  const capture = async (name: string, modal = true) => {
    await page.evaluate(() => document.fonts.ready);
    if (modal) await page.locator(".grants-modal").evaluate((node) => { node.scrollTop = 0; });
    await (modal ? page.locator(".grants-modal") : page.getByRole("banner")).screenshot({
      path: path.join(output, `${name}.png`), animations: "disabled",
    });
  };
  const selectSkill = async (name: string) => {
    await page.getByTestId("permission-skill-select").selectOption(name);
    await expect(page.locator("#grantsDetails")).toBeVisible();
    await expect(page.locator("#skillDependencyStatus")).not.toHaveText("Checking dependencies...");
    await expect(page.locator("#grantsSearchStatus")).not.toHaveText("Searching...");
  };

  await openApp(page);
  await expect(page.getByTestId("grants-button")).toBeVisible();
  await capture("01-entry", false);
  await page.getByTestId("grants-button").click();
  await selectSkill("training-demo");
  await expect(page.locator("#grantsList")).toContainText("e2e@example.com");
  await capture("02-private-skill");

  await page.locator("#grantsAddInput").fill("alice");
  await expect(page.locator("#grantsPrincipalSelect option")).toHaveText(["alice@example.com"]);
  await page.locator("#grantsPrincipalSelect").selectOption("alice@example.com");
  await expect(page.locator("#grantsAddBtn")).toBeEnabled();
  await capture("03-user-recipient");
  await page.locator("#grantsAddBtn").click();
  await expect(page.locator("#grantsList")).toContainText("alice@example.com");
  await page.locator("#grantsPrincipalType").selectOption("X");
  await page.locator("#grantsAddInput").fill("HR");
  await expect(page.locator("#grantsPrincipalSelect option")).toHaveText(["HR Skill Users"]);
  await page.locator("#grantsPrincipalSelect").selectOption("HR Skill Users");
  await page.locator("#grantsAddBtn").click();
  await expect(page.locator("#grantsList")).toContainText("HR Skill Users");
  await expect(page.locator("#grantsSearchStatus")).not.toHaveText("Searching...");
  await capture("04-group-access");

  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#grantsPublicToggle").check();
  await expect(page.locator("#grantsPublicHint")).toContainText("Existing grants are retained");
  await expect(page.locator("#grantsList")).toContainText("alice@example.com");
  await capture("05-public-skill");
  await page.locator("#grantsPublicToggle").uncheck();
  await expect(page.locator("#grantsPublicHint")).toContainText("Only the listed identities");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove access for alice@example.com" }).click();
  await expect(page.locator("#grantsList .grant-upn")).toHaveText(["e2e@example.com", "HR Skill Users"]);
  await capture("06-access-removed");

  await selectSkill("leave-request");
  await expect(page.locator("#skillDependents")).toContainText("leave-workflow -> leave-request");
  await page.locator("#skillDeleteConfirm").fill("leave-request");
  await expect(page.locator("#skillDeleteBtn")).toBeDisabled();
  await capture("07-deletion-blocked");
  await selectSkill("leave-workflow");
  await expect(page.locator("#skillDependencies")).toHaveText("leave-request");
  await capture("08-outgoing-dependency");

  await selectSkill("training-demo");
  await page.locator("#skillDeleteConfirm").fill("training-demo");
  await expect(page.locator("#skillDeleteBtn")).toBeEnabled();
  await capture("09-delete-confirmation");
  await page.setViewportSize({ width: 390, height: 844 });
  const modal = page.locator(".grants-modal");
  expect(await modal.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  const bounds = await modal.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1280, height: 1400 });
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#skillDeleteBtn").click();
  await expect(page.locator("#grantsStatus")).toContainText("Deleted training-demo. History and conversations retained.");
  await capture("10-deleted");
  expect((await request.get("/api/skills/leave-request")).ok()).toBeTruthy();
});