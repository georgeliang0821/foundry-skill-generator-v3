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
  await expect(
    page.getByTestId("target-skill-select").locator("optgroup[label='Capability'] > option[value='e2e-existing-skill']"),
  ).toHaveCount(1);
  await page.getByTestId("target-skill-select").selectOption("e2e-existing-skill");
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("capability");
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

test("picks up the scenario kind from the selected skill", async ({ page, request }) => {
  await seedSkill(request, "e2e-leave-workflow", "scenario");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(
    page.getByTestId("target-skill-select").locator("optgroup[label='Scenario (orchestration)'] > option[value='e2e-leave-workflow']"),
  ).toHaveCount(1);
  // Kind is an output here, so it stays blank until a skill decides it.
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("");
  await page.getByTestId("target-skill-select").selectOption("e2e-leave-workflow");
  await expect(page.getByTestId("skill-kind-select")).toHaveValue("scenario");

  // Regression: the disabled Kind select used to be posted as-is, so starting a
  // modify session on a scenario skill failed the backend's mismatch check.
  await startNewSession(page, "modify");
  // The session id still shows the boot session until the new one renders.
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");

  const session = await readSession(request, await currentSessionId(page));
  expect(session.remote_skill_id).toBe("e2e-leave-workflow");
  expect(session.skill_kind).toBe("scenario");
  expect(session.children).toEqual(["hr-leave-system"]);
});

test("keeps the header controls on one row in modify mode", async ({ page, request }) => {
  await seedSkill(request, "e2e-existing-skill");
  await openApp(page);

  await page.getByTestId("mode-select").selectOption("modify");
  await expect(page.getByTestId("target-skill-select")).toBeVisible();

  // The Skill group only exists in modify mode, so this is where the row used to wrap.
  const mode = await page.getByTestId("mode-select").boundingBox();
  const newSession = await page.getByTestId("new-session-button").boundingBox();
  expect(Math.abs((mode?.y ?? 0) - (newSession?.y ?? 0))).toBeLessThan(8);
});
