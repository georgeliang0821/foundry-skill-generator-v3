import { expect, test } from "@playwright/test";
import { openApp, openTab, sendChat } from "./helpers/app";
import { currentSessionId, readSession, resetE2E, seedSkill, setScenario } from "./helpers/e2eApi";

test.beforeEach(async ({ request }) => {
  await resetE2E(request);
  await seedSkill(request, "hr-leave-system");
  await setScenario(request, "scenario_skill_happy_path");
});

test("creates, validates, saves, and tests a scenario skill", async ({ page, request }) => {
  await openApp(page);
  await expect(page.getByTestId("session-id")).not.toHaveText("No session");
  await page.getByTestId("skill-kind-select").selectOption("scenario");
  await page.getByTestId("new-session-button").click();
  await expect(page.getByTestId("session-id")).not.toHaveText("No session");

  await page.getByTestId("add-material-row-button").click();
  await page.getByTestId("material-input").fill("Create a leave workflow that delegates HR operations.");
  await page.getByTestId("attach-material-button").click();
  await sendChat(page, "Create a deterministic E2E leave scenario.");

  await openTab(page, "checklist");
  await expect(page.getByTestId("checklist")).toContainText("Delegation confirmed");
  await expect(page.getByTestId("checklist")).not.toContainText("Variables verified");
  await expect(page.getByTestId("delegation-panel")).toBeVisible();
  await expect(page.getByTestId("delegation-row").locator("select")).toHaveValue("hr-leave-system");

  await openTab(page, "files");
  await expect(page.getByTestId("file-editor")).toHaveValue(/skill_type: scenario-orchestration/);
  await expect(page.getByTestId("file-editor")).toHaveValue(/children:\s+\- hr-leave-system/);

  await openTab(page, "topology");
  await expect(page.getByTestId("topology-results").locator("[data-topology-rule]")).toHaveCount(19);
  await expect(page.getByTestId("topology-results").locator(".topology-rule.failed")).toHaveCount(0);

  await openTab(page, "files");
  await page.getByTestId("accept-draft-button").click();
  await expect(page.getByTestId("skill-binding-status")).toContainText("e2e-leave-workflow");

  const sessionId = await currentSessionId(page);
  let session = await readSession(request, sessionId);
  expect(session.skill_kind).toBe("scenario");
  expect(session.children).toEqual(["hr-leave-system"]);
  expect(session.remote_skill_id).toBe("e2e-leave-workflow");

  const skillsResponse = await request.get("/api/skills");
  expect(skillsResponse.ok()).toBeTruthy();
  const skills = await skillsResponse.json();
  expect(skills.find((skill: { name: string }) => skill.name === "hr-leave-system")?.is_internal).toBe(true);

  await openTab(page, "tests");
  await page.getByTestId("run-tests-button").click();
  await expect(page.getByTestId("test-results")).toContainText("L1 - Static topology (T1-T10)");
  await expect(page.getByTestId("test-results")).toContainText("L2 - Parent absence from skills_referenced");
  await expect(page.getByTestId("test-results")).toContainText("L3 - Child reachability");
  await expect(page.getByTestId("test-results")).toContainText("Not verified by this run.");

  session = await readSession(request, sessionId);
  expect(session.test_runs).toHaveLength(1);
  expect(session.test_runs[0].scenario_layers.map((layer: { layer: string }) => layer.layer)).toEqual(["L1", "L2", "L3"]);
  expect(session.test_runs[0].scenario_layers.every((layer: { passed: boolean }) => layer.passed)).toBe(true);
});