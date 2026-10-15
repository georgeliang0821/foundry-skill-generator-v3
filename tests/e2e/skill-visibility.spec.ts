import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("permission entry works without a session and shows an empty skill state", async ({ page }) => {
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await expect(page.locator("#grantsStatus")).toHaveText("No skills available for management.");
  await expect(page.getByTestId("permission-skill-select")).toBeDisabled();
  await expect(page.locator("#grantsDetails")).toBeHidden();
});

test("permission selector excludes other creators and principal search recovers after failure", async ({ page, request }) => {
  await seedSkill(request, "owned-skill");
  expect((await request.post("/api/e2e/seed-skill", { data: { name: "other-skill", user_upn: "other@example.com" } })).ok()).toBeTruthy();
  expect((await request.patch("/api/skills/other-skill/visibility", {
    headers: { "X-Test-UPN": "other@example.com" }, data: { is_public: true },
  })).ok()).toBeTruthy();
  let failSearch = true;
  await page.route("**/api/permissions/principals?*", (route) => failSearch
    ? route.fulfill({ status: 503, json: { detail: "SQL principal search is unavailable." } })
    : route.fulfill({ json: { principals: [{ name: "alice@contoso.com", type: "E" }] } }));
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await expect(page.getByTestId("permission-skill-select").locator("option")).toHaveText(["Select a skill", "owned-skill"]);
  await page.getByTestId("permission-skill-select").selectOption("owned-skill");
  await expect(page.locator("#grantsSearchStatus")).toContainText("SQL principal search is unavailable");
  await expect(page.locator("#grantsAddBtn")).toBeDisabled();
  failSearch = false;
  await page.locator("#grantsAddInput").fill("alice");
  await expect(page.locator("#grantsPrincipalSelect option")).toHaveText(["alice@contoso.com"]);
  await page.locator("#grantsPrincipalSelect").selectOption("alice@contoso.com");
  await expect(page.locator("#grantsAddBtn")).toBeEnabled();
});

test("a new skill is saved without a publication option", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await expect(page.getByTestId("draft-public-toggle")).toHaveCount(0);
  await openTab(page, "files");
  await page.getByTestId("accept-draft-button").click();

  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await expect(page.getByTestId("skill-binding-status")).not.toContainText("public");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-calendar-skill");
  await expect(page.locator("#grantsModal")).toBeHidden();
  await page.getByTestId("skill-binding-status").getByRole("button", { name: "Manage Skills" }).click();
  await expect(page.getByTestId("permission-skill-select")).toHaveValue("e2e-calendar-skill");
});

test("management shows both dependency directions and blocks referenced capability deletion", async ({ page, request }) => {
  await seedSkill(request, "child");
  await seedSkill(request, "parent", "scenario", { skill_md: "---\nname: parent\ndescription: Parent\nmetadata:\n  children: [child]\n---\n" });
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await expect(page.locator("#grantsModalTitle")).toHaveText("Manage Skills");
  await page.getByTestId("permission-skill-select").selectOption("child");
  await expect(page.locator("#skillDependents")).toContainText("parent -> child");
  await page.locator("#skillDeleteConfirm").fill("child");
  await expect(page.locator("#skillDeleteBtn")).toBeDisabled();
  await page.getByTestId("permission-skill-select").selectOption("parent");
  await expect(page.locator("#skillDependencies")).toHaveText("child");
  await expect(page.locator("#skillDependencyStatus")).toHaveText("No incoming dependencies.");
  await page.locator("#skillDeleteConfirm").fill("wrong-name");
  await expect(page.locator("#skillDeleteBtn")).toBeDisabled();
  await page.locator("#skillDeleteConfirm").fill("parent");
  await expect(page.locator("#skillDeleteBtn")).toBeEnabled();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.locator("#skillDeleteBtn").click();
  await expect(page.getByTestId("permission-skill-select")).toHaveValue("parent");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#skillDeleteConfirm").scrollIntoViewIfNeeded();
  expect(await page.locator(".grants-modal").evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.screenshot({ path: "test-results/manage-skills-delete-mobile.png" });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: "test-results/manage-skills-delete-desktop.png" });
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#skillDeleteBtn").click();
  await expect(page.locator("#grantsStatus")).toContainText("Deleted parent.");
  await expect(page.getByTestId("permission-skill-select").locator("option")).toHaveText(["Select a skill", "child"]);
  expect((await request.get("/api/skills/child")).ok()).toBeTruthy();
});

for (const choice of ["make_capability", "keep_internal", "cancel"]) {
  test(`scenario deletion handles internal child choice: ${choice}`, async ({ page, request }) => {
    await seedSkill(request, "child");
    await seedSkill(request, "parent", "scenario", {
      skill_md: "---\nname: parent\ndescription: Parent\nmetadata:\n  children: [child]\n---\n",
    });
    const payloads: Record<string, unknown>[] = [];
    await page.route("**/api/skills/parent", async (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      payloads.push(route.request().postDataJSON());
      if (payloads.length > 1) return route.continue();
      await route.fulfill({ status: 409, json: { detail: {
        kind: "orphaned_children", children: ["child"], recoverable: true,
        message: "Choose how to handle internal children.",
      } } });
    });
    await openApp(page);
    await page.getByTestId("grants-button").click();
    await page.getByTestId("permission-skill-select").selectOption("parent");
    await expect(page.locator("#skillDependencyStatus")).toHaveText("No incoming dependencies.");
    await page.locator("#skillDeleteConfirm").fill("parent");
    const dialogs: string[] = [];
    page.on("dialog", async (dialog) => {
      dialogs.push(dialog.message());
      if (dialogs.length === 1 || (dialogs.length === 2 && choice === "make_capability")
          || (dialogs.length === 3 && choice === "keep_internal")) {
        await dialog.accept();
      } else {
        await dialog.dismiss();
      }
    });
    await page.locator("#skillDeleteBtn").click();
    if (choice === "cancel") {
      await expect(page.locator("#skillDeleteBtn")).toBeEnabled();
      await expect(page.getByTestId("permission-skill-select")).toHaveValue("parent");
      expect(payloads).toHaveLength(1);
      expect((await request.get("/api/skills/parent")).ok()).toBeTruthy();
    } else {
      await expect(page.locator("#grantsStatus")).toContainText("Deleted parent.");
      expect(payloads).toHaveLength(2);
      expect(payloads[1].orphan_action).toBe(choice);
    }
    expect(dialogs).toHaveLength(choice === "make_capability" ? 2 : 3);
    expect(dialogs[1]).toContain("INTERNAL");
    expect(dialogs[1]).toContain("child");
    await expect(page.locator("#grantsError")).toBeEmpty();
    expect((await request.get("/api/skills/child")).ok()).toBeTruthy();
  });
}

test("deleting a bound skill retains its conversation and disables saving", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  const sid = await currentSessionId(page);
  const before = await readSession(request, sid);
  await page.getByTestId("skill-binding-status").getByRole("button", { name: "Manage Skills" }).click();
  await expect(page.locator("#skillDependencyStatus")).toHaveText("No incoming dependencies.");
  await page.locator("#skillDeleteConfirm").fill("e2e-calendar-skill");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#skillDeleteBtn").click();
  await expect(page.locator("#grantsStatus")).toContainText("Deleted e2e-calendar-skill.");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("skill-binding-status")).toContainText("Skill deleted");
  await expect(page.locator("#saveDraftBtn")).toBeDisabled();
  expect(await readSession(request, sid)).toEqual(before);
  expect((await request.get(`/api/sessions/${sid}/binding`)).ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator("#sessionList")).toBeEnabled();
  await page.locator("#sessionList").selectOption(sid);
  await expect(page.getByTestId("skill-binding-status")).toContainText("Skill deleted");
  await expect(page.locator("#saveDraftBtn")).toBeDisabled();
});

test("dependency and deletion failures remain visible and never report success", async ({ page, request }) => {
  await seedSkill(request, "target");
  let checkFails = true;
  await page.route("**/api/skills/target/dependencies", (route) => checkFails
    ? route.fulfill({ status: 503, json: { detail: "Dependency check incomplete" } })
    : route.continue());
  await page.route("**/api/skills/target", (route) => route.request().method() === "DELETE"
    ? route.fulfill({ status: 503, json: { detail: "Final backup failed. Nothing was deleted." } })
    : route.continue());
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await page.getByTestId("permission-skill-select").selectOption("target");
  await expect(page.locator("#skillDependencyStatus")).toContainText("Deletion unavailable");
  await page.locator("#skillDeleteConfirm").fill("target");
  await expect(page.locator("#skillDeleteBtn")).toBeDisabled();
  checkFails = false;
  await page.getByTestId("permission-skill-select").selectOption("");
  await page.getByTestId("permission-skill-select").selectOption("target");
  await expect(page.locator("#skillDependencyStatus")).toHaveText("No incoming dependencies.");
  await page.locator("#skillDeleteConfirm").fill("target");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#skillDeleteBtn").click();
  await expect(page.locator("#grantsError")).toContainText("Final backup failed");
  await expect(page.getByTestId("permission-skill-select")).toHaveValue("target");
});

test("permission management publishes and restores retained grants independently of sessions", async ({ page, request }) => {
  await seedSkill(request, "owned-skill");
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await page.getByTestId("permission-skill-select").selectOption("owned-skill");
  await expect(page.locator("#grantsList")).toContainText("e2e@example.com");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#grantsPublicToggle").check();
  await expect(page.locator("#grantsAddForm")).toBeVisible();
  await expect(page.locator("#grantsPrincipalType option")).toHaveText(["User", "Group"]);
  await expect(page.locator("#grantsPublicHint")).toContainText("Existing grants are retained");
  expect((await (await request.get("/api/skills/owned-skill/grants")).json()).is_public).toBe(true);
  await page.locator("#grantsPublicToggle").uncheck();
  await expect(page.locator("#grantsAddForm")).toBeVisible();
  await expect(page.locator("#grantsList")).toContainText("e2e@example.com");
  expect((await (await request.get("/api/skills/owned-skill/grants")).json()).is_public).toBe(false);
});

test("cancelling publication leaves the saved skill unchanged", async ({ page, request }) => {
  await seedSkill(request, "owned-skill");
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await page.getByTestId("permission-skill-select").selectOption("owned-skill");
  await expect(page.locator("#grantsDetails")).toBeVisible();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.locator("#grantsPublicToggle").click();
  await expect(page.locator("#grantsPublicToggle")).not.toBeChecked();
  expect((await (await request.get("/api/skills/owned-skill/grants")).json()).is_public).toBe(false);
  await page.keyboard.press("Escape");
  await expect(page.locator("#grantsModal")).toBeHidden();
  await expect(page.getByTestId("grants-button")).toBeFocused();
});

test("public skill permits User or Group assignment and fits desktop and mobile", async ({ page, request }) => {
  await seedSkill(request, "owned-skill");
  expect((await request.patch("/api/skills/owned-skill/visibility", { data: { is_public: true } })).ok()).toBeTruthy();
  await page.route("**/api/permissions/principals?*", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const principal = params.get("principal_type") === "X"
      ? { name: "EAA", type: "X" } : { name: "alice@contoso.com", type: "E" };
    await route.fulfill({ json: { principals: principal.name.toLowerCase().includes((params.get("q") || "").toLowerCase()) ? [principal] : [] } });
  });
  await page.route("**/api/skills/owned-skill/grants", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const payload = route.request().postDataJSON();
    expect(payload).toEqual({ user_upn: "EAA", principal_type: "X" });
    const result = await request.post("/api/skills/owned-skill/grants", { data: { user_upn: payload.user_upn } });
    await route.fulfill({ response: result });
  });
  await openApp(page);
  await page.getByTestId("grants-button").click();
  await page.getByTestId("permission-skill-select").selectOption("owned-skill");
  await expect(page.locator("#grantsPublicToggle")).toBeChecked();
  await expect(page.locator("#grantsAddForm")).toBeVisible();
  await expect(page.locator("#grantsPrincipalType option")).toHaveText(["User", "Group"]);
  await expect(page.locator("#grantsPrincipalSelect option")).toHaveText(["alice@contoso.com"]);
  await page.locator("#grantsPrincipalType").selectOption("X");
  await page.locator("#grantsAddInput").fill("EA");
  await expect(page.locator("#grantsPrincipalSelect option")).toHaveText(["EAA"]);
  await page.locator("#grantsPrincipalSelect").selectOption("EAA");
  await page.getByRole("button", { name: "Add access" }).click();
  await expect(page.locator("#grantsList .grant-upn")).toHaveText(["e2e@example.com", "EAA"]);
  await page.screenshot({ path: "test-results/assign-permission-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  const modal = page.locator(".grants-modal");
  await expect(modal).toBeVisible();
  const bounds = await modal.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(await modal.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.screenshot({ path: "test-results/assign-permission-mobile.png" });
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove access for EAA" }).click();
  await expect(page.locator("#grantsList .grant-upn")).toHaveText(["e2e@example.com"]);
});
