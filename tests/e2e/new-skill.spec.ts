import { expect, test } from "@playwright/test";
import { createDraftWithFakeAgent, openApp, openTab } from "./helpers/app";
import { currentSessionId, readSession, resetE2E } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("creates a new skill through chat and saves it locally", async ({ page, request }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  await openTab(page, "checklist");
  await expect(page.getByTestId("checklist")).toContainText("Skill definition");
  await expect(page.getByTestId("checklist")).toContainText("Routing & uniqueness");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/## Ground Rules/);
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-calendar-skill");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-calendar-skill");
  expect(session.current_skill.skill_md).toContain("e2e-calendar-skill");
  expect(session.current_skill.skill_md).toContain("e2e result");
  await expect(page.getByTestId("markdown-preview")).toContainText("Ground Rules");
  await expect(page.getByTestId("markdown-preview").getByRole("heading", { name: "Ground Rules" })).toBeVisible();
  await expect(page.getByTestId("markdown-preview")).not.toContainText("## Ground Rules");
  await expect(page.getByTestId("markdown-preview").locator("pre code").filter({ hasText: "def run" })).toBeVisible();
});

test("draft card prefills the name from the proposed frontmatter", async ({ page }) => {
  await openApp(page);
  await createDraftWithFakeAgent(page);

  // The card renders on the tool_call event, before the session state_update
  // arrives, so it must read the name off the proposed draft itself.
  await expect(page.getByTestId("draft-name-input")).toHaveValue("e2e-calendar-skill");

  const toggle = await page.getByTestId("draft-public-toggle").boundingBox();
  expect(toggle!.width).toBeLessThan(40);
});
