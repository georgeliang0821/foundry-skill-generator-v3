import { expect, test } from "@playwright/test";
import { openApp, openTab, sendChat, startNewSession } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
});

test("loads and modifies an existing local skill", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await setScenario(request, "modify_existing");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(page.getByTestId("target-skill-select")).toBeVisible();
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await startNewSession(page, "modify");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/e2e-existing-skill/);

  await sendChat(page, "Prepare this existing skill for modification.");
  await sendChat(page, "Modify this existing skill.");
  await page.getByTestId("patch-card").last().getByRole("button", { name: "Accept patch" }).click();
  await expect(page.getByTestId("file-editor")).toHaveValue(/E2E refined selection boundary/);

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-existing-skill");
  expect(session.current_skill.skill_md).toContain("E2E refined selection boundary");
});
