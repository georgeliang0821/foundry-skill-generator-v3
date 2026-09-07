import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("a new skill stays private unless the author opts in", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await expect(page.getByTestId("draft-public-toggle")).not.toBeChecked();
  await openTab(page, "files");
  await page.getByTestId("accept-draft-button").click();

  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");
  await expect(page.getByTestId("skill-binding-status")).not.toContainText("public");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-calendar-skill");
});

test("checking public and confirming publishes the saved skill", async ({ page }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await page.getByTestId("draft-public-toggle").check();
  page.once("dialog", (dialog) => dialog.accept());

  await page.getByRole("button", { name: "Accept SKILL.md" }).click();

  await expect(page.getByTestId("skill-binding-status")).toContainText("public");
});

test("backing out of the confirmation abandons the accept entirely", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await page.getByTestId("draft-public-toggle").check();
  page.once("dialog", (dialog) => dialog.dismiss());

  await page.getByRole("button", { name: "Accept SKILL.md" }).click();

  // Nothing was saved, so the card is still waiting and the box stays checked.
  await expect(page.getByTestId("draft-card")).toBeVisible();
  await expect(page.getByTestId("draft-public-toggle")).toBeChecked();

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBeFalsy();
});
